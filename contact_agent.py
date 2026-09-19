"""The brain. Finds PEOPLE and their contact details, saves them to results.csv"""

import os
import re
import csv
import json
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from ddgs import DDGS
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.getenv("OPENROUTER_API_KEY")
MODEL = "openrouter/free"

CSV_FILE = "results.csv"
FIELDS = ["target", "name", "role", "company", "email", "phone", "linkedin",
          "confidence", "notes", "source_url", "found_at"]

MAX_PAGES = 12          # protects your free daily request limit
PAUSE_SECONDS = 2


# ---------------------------------------------------------------- storage

def _fix_header_if_old():
    """If results.csv was written with the older column set, rewrite it with the new one."""
    if not os.path.exists(CSV_FILE):
        return
    with open(CSV_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames == FIELDS:
            return
        old_rows = list(reader)
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        for row in old_rows:
            writer.writerow({k: (row.get(k) or "") for k in FIELDS})


def read_rows():
    _fix_header_if_old()
    if not os.path.exists(CSV_FILE):
        return []
    with open(CSV_FILE, newline="", encoding="utf-8") as f:
        return [{k: (row.get(k) or "") for k in FIELDS} for row in csv.DictReader(f)]


def append_rows(rows):
    _fix_header_if_old()
    file_is_new = not os.path.exists(CSV_FILE)
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if file_is_new:
            writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in FIELDS})


# ---------------------------------------------------------------- the LLM

def ask_llm(prompt):
    r = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={
            "model": MODEL,
            "messages": [
                {"role": "system",
                 "content": "You extract contact details about people from web page text. "
                            "You only report details that literally appear in the text. You never guess."},
                {"role": "user", "content": prompt},
            ],
        },
        timeout=120,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def parse_json(raw, fallback):
    text = raw.strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return fallback


# ---------------------------------------------------------------- finding patterns in text

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?:\+\d{1,3}[\s.\-]?)?(?:\(\d{3}\)|\d{3})[\s.\-]\d{3}[\s.\-]\d{4}\b")
LINKEDIN_RE = re.compile(r"(?:https?://)?(?:[a-z]{2,3}\.)?linkedin\.com/in/[A-Za-z0-9\-_%]+")

JUNK_EMAIL_PARTS = ("example.com", "yourdomain", "sentry.io", "wixpress", "@2x.png", "domain.com")


def deobfuscate(text):
    """Turn 'anurag [at] example [dot] com' into a real address."""
    text = re.sub(r"\s*[\(\[\{]\s*at\s*[\)\]\}]\s*", "@", text, flags=re.I)
    text = re.sub(r"\s*[\(\[\{]\s*dot\s*[\)\]\}]\s*", ".", text, flags=re.I)
    return text


def find_patterns(text):
    emails = [e for e in set(EMAIL_RE.findall(text))
              if not any(j in e.lower() for j in JUNK_EMAIL_PARTS)]
    return {
        "emails": emails[:25],
        "phones": list(set(PHONE_RE.findall(text)))[:25],
        "linkedins": list(set(LINKEDIN_RE.findall(text)))[:25],
    }


# ---------------------------------------------------------------- reading pages

def fetch_page(url, max_chars=7000):
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer"]):
            tag.decompose()

        # mailto: and linkedin links hide in href attributes, not visible text
        links = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("mailto:") or "linkedin.com/in/" in href:
                links.append(href.replace("mailto:", ""))

        body = " ".join(soup.get_text(" ").split())[:max_chars]
        return deobfuscate(body + " " + " ".join(links[:40]))
    except Exception:
        return ""


# ---------------------------------------------------------------- the prompts

def plan_queries(target):
    prompt = f"""I am looking for the contact details of a PERSON (or people) matching this description:
"{target}"

Write 4 web search queries that would surface their name, work email, phone number, or LinkedIn profile.
Make the queries different from each other. Useful angles include:
- a site:linkedin.com/in query
- the company's team / leadership / staff directory page
- conference speaker bios, press releases, or published papers
- an email pattern query using the company domain

Reply with ONLY a JSON array of 4 strings. No markdown, no explanation."""
    return parse_json(ask_llm(prompt), fallback=[
        f'site:linkedin.com/in "{target}"',
        f'"{target}" email contact',
        f'"{target}" team OR leadership OR staff directory',
        f'"{target}" phone',
    ])


def extract_people(target, url, page_text, hints):
    prompt = f"""Below is text from the web page {url}.

--- PAGE TEXT ---
{page_text}
--- END OF PAGE TEXT ---

Email addresses found in that text by a pattern matcher: {hints['emails'] or 'none'}
Phone numbers found: {hints['phones'] or 'none'}
LinkedIn profiles found: {hints['linkedins'] or 'none'}

I am researching: {target}

List every PERSON on this page who is relevant to that search and for whom the page gives
at least one of: an email, a phone number, or a LinkedIn profile.

Reply with ONLY this JSON and nothing else:
{{"people": [
  {{"name": "", "role": "", "company": "", "email": "", "phone": "", "linkedin": "", "confidence": "high", "notes": ""}}
]}}

Rules:
- One object per person. Return an empty list if nobody on the page qualifies.
- Use "" for any detail the page does not give.
- NEVER construct or guess an email address. Only copy one that appears in the text above.
- Do not include generic mailboxes such as info@, sales@, support@, hello@, careers@, privacy@.
- confidence: "high" if the page clearly ties the detail to that named person,
  "medium" if it is nearby but not explicit, "low" if you are unsure.
- notes: one short phrase on where the detail came from, e.g. "listed on team page"."""
    data = parse_json(ask_llm(prompt), fallback={"people": []})
    if isinstance(data, list):
        return data
    return data.get("people", []) if isinstance(data, dict) else []


GENERIC_PREFIXES = ("info@", "sales@", "support@", "hello@", "contact@", "admin@",
                    "careers@", "jobs@", "press@", "privacy@", "legal@", "noreply@", "no-reply@")


def validate(person, page_text):
    """Throw away anything the model made up. Returns the cleaned person, or None."""
    lower_page = page_text.lower()

    email = (person.get("email") or "").strip()
    if email:
        if email.lower() not in lower_page or email.lower().startswith(GENERIC_PREFIXES):
            person["email"] = ""
            person["notes"] = ((person.get("notes") or "") + " [email discarded]").strip()

    linkedin = (person.get("linkedin") or "").strip()
    if linkedin and "linkedin.com/in/" not in linkedin.lower():
        person["linkedin"] = ""

    if not (person.get("name") or "").strip():
        return None
    if not (person.get("email") or person.get("phone") or person.get("linkedin")):
        return None
    return person


# ---------------------------------------------------------------- the run

def run_agent(target, log=print):
    log(f"Planning searches for: {target}")
    queries = plan_queries(target)
    for q in queries:
        log(f"   plan: {q}")

    existing = read_rows()
    seen_urls = {r["source_url"] for r in existing if r["target"] == target}
    seen_people = {(r["name"].strip().lower(), r["email"].strip().lower()) for r in existing}

    findings = []
    pages_read = 0

    for q in queries:
        if pages_read >= MAX_PAGES:
            break
        log(f"Searching: {q}")
        try:
            hits = list(DDGS().text(q, max_results=5))
        except Exception as e:
            log(f"   search failed: {e}")
            continue

        for hit in hits:
            if pages_read >= MAX_PAGES:
                log("   page limit reached, stopping")
                break

            url = (hit.get("href") or "").strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            # LinkedIn blocks scrapers — use the search snippet instead of fetching
            if "linkedin.com/in/" in url:
                text = f"{hit.get('title', '')} {hit.get('body', '')} {url}"
                log(f"   linkedin result: {url}")
            else:
                log(f"   reading {url}")
                text = fetch_page(url)
                if len(text) < 200:
                    continue

            hints = find_patterns(text)
            pages_read += 1

            for person in extract_people(target, url, text, hints):
                if not isinstance(person, dict):
                    continue
                person = validate(person, text)
                if not person:
                    continue

                key = (person["name"].strip().lower(), (person.get("email") or "").strip().lower())
                if key in seen_people:
                    continue
                seen_people.add(key)

                person["target"] = target
                person["source_url"] = url
                person["found_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                findings.append(person)
                log(f"      {person['name']} — {person.get('email') or person.get('phone') or person.get('linkedin')}")

            time.sleep(PAUSE_SECONDS)

    append_rows(findings)
    log(f"Finished. Read {pages_read} pages, saved {len(findings)} new people.")
    return findings


if __name__ == "__main__":
    run_agent(input("Who are you looking for? "))