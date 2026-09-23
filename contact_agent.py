"""The brain. Finds PEOPLE and their contact details, saves them to results.xlsx"""

import os
import re
import json
import time
import uuid
import threading
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from ddgs import DDGS
from dotenv import load_dotenv

import store

load_dotenv()
API_KEY = os.getenv("OPENROUTER_API_KEY")
MODEL = "openrouter/free"

FIELDS = ["row_id", "search_date", "target", "country", "name", "role", "company",
          "email", "phone", "linkedin", "contacted", "comment", "confidence",
          "notes", "source_url", "found_at"]

DISPLAY_FIELDS = ["comment", "search_date", "target", "country", "name", "role",
                  "company", "email", "phone", "linkedin", "confidence", "notes",
                  "source_url", "found_at"]

EDITABLE_FIELDS = {"phone", "comment", "email", "name", "role", "company"}

MAX_PAGES = 12
PAUSE_SECONDS = 2

_lock = threading.Lock()


# ---------------------------------------------------------------- countries

COUNTRIES = {
    "":   ("Worldwide (no country filter)", "wt-wt"),
    "US": ("United States",   "us-en"),
    "CA": ("Canada",          "ca-en"),
    "GB": ("United Kingdom",  "uk-en"),
    "IE": ("Ireland",         "ie-en"),
    "DE": ("Germany",         "de-de"),
    "FR": ("France",          "fr-fr"),
    "NL": ("Netherlands",     "nl-nl"),
    "ES": ("Spain",           "es-es"),
    "IT": ("Italy",           "it-it"),
    "SE": ("Sweden",          "se-sv"),
    "CH": ("Switzerland",     "ch-de"),
    "PL": ("Poland",          "pl-pl"),
    "IN": ("India",           "in-en"),
    "SG": ("Singapore",       "sg-en"),
    "AE": ("United Arab Emirates", "ae-en"),
    "AU": ("Australia",       "au-en"),
    "NZ": ("New Zealand",     "nz-en"),
    "JP": ("Japan",           "jp-jp"),
    "KR": ("South Korea",     "kr-kr"),
    "PH": ("Philippines",     "ph-en"),
    "MY": ("Malaysia",        "my-en"),
    "BR": ("Brazil",          "br-pt"),
    "MX": ("Mexico",          "mx-es"),
    "ZA": ("South Africa",    "za-en"),
}


def country_name(code):
    return COUNTRIES.get(code, COUNTRIES[""])[0]


def country_region(code):
    return COUNTRIES.get(code, COUNTRIES[""])[1]


# ---------------------------------------------------------------- links

def normalize_linkedin(value):
    v = (value or "").strip()
    if "linkedin.com/in/" not in v.lower():
        return ""
    if not v.lower().startswith(("http://", "https://")):
        v = "https://" + v.lstrip("/")
    return v.split("?")[0].rstrip("/")


# ---------------------------------------------------------------- storage

def _load():
    return store.read_sheet(store.CONTACTS_SHEET, FIELDS)


def _save(rows):
    store.write_sheet(store.CONTACTS_SHEET, FIELDS, rows)


def clear_all():
    store.clear_sheet(store.CONTACTS_SHEET)


def _migrate(rows):
    changed = False
    for r in rows:
        if not r.get("row_id"):
            r["row_id"] = uuid.uuid4().hex[:12]
            changed = True
        if r.get("contacted") not in ("yes", "no"):
            r["contacted"] = "no"
            changed = True
        for col in ("country", "comment"):
            if r.get(col) is None:
                r[col] = ""
                changed = True
        if not r.get("search_date"):
            r["search_date"] = (r.get("found_at") or "")[:10]
            changed = True
        fixed = normalize_linkedin(r.get("linkedin"))
        if fixed != (r.get("linkedin") or ""):
            r["linkedin"] = fixed
            changed = True
    return changed


def read_rows():
    with _lock:
        rows = _load()
        if _migrate(rows):
            _save(rows)
        return rows


def append_rows(new_rows):
    """Newest findings go to the TOP, pushing older rows down."""
    with _lock:
        rows = _load()
        _migrate(rows)
        rows = list(new_rows) + rows
        _save(rows)


def delete_row(row_id):
    with _lock:
        rows = _load()
        _migrate(rows)
        kept = [r for r in rows if r["row_id"] != row_id]
        if len(kept) == len(rows):
            return False
        _save(kept)
        return True


def toggle_contacted(row_id):
    with _lock:
        rows = _load()
        _migrate(rows)
        for r in rows:
            if r["row_id"] == row_id:
                r["contacted"] = "no" if r["contacted"] == "yes" else "yes"
                _save(rows)
                return r["contacted"]
        return None


def update_field(row_id, field, value):
    if field not in EDITABLE_FIELDS:
        return False
    with _lock:
        rows = _load()
        _migrate(rows)
        for r in rows:
            if r["row_id"] == row_id:
                r[field] = (value or "").strip()
                if field == "linkedin":
                    r[field] = normalize_linkedin(r[field])
                _save(rows)
                return True
        return False


# ---------------------------------------------------------------- the LLM

def ask_llm(prompt, retries=2):
    """Ask the LLM. Returns text, or '' if the model gave us nothing usable."""
    last_problem = ""

    for attempt in range(retries + 1):
        try:
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
            data = r.json()
        except Exception as e:
            last_problem = str(e)
            time.sleep(2)
            continue

        if not data.get("choices"):
            last_problem = f"no choices in reply: {str(data)[:200]}"
            time.sleep(2)
            continue

        message = data["choices"][0].get("message") or {}
        content = message.get("content")

        if not content:
            content = message.get("reasoning")

        if isinstance(content, list):
            content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))

        if content and content.strip():
            return content

        last_problem = "model returned empty content"
        time.sleep(2)

    print(f"   LLM gave nothing after {retries + 1} tries ({last_problem})")
    return ""


def parse_json(raw, fallback):
    if not raw or not isinstance(raw, str):
        return fallback
    text = raw.strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        if opener in text and closer in text:
            start, end = text.find(opener), text.rfind(closer)
            if start < end:
                text = text[start:end + 1]
                break
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return fallback


# ---------------------------------------------------------------- patterns

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?:\+\d{1,3}[\s.\-]?)?(?:\(\d{3}\)|\d{3})[\s.\-]\d{3}[\s.\-]\d{4}\b")
LINKEDIN_RE = re.compile(r"(?:https?://)?(?:[a-z]{2,3}\.)?linkedin\.com/in/[A-Za-z0-9\-_%]+")

JUNK_EMAIL_PARTS = ("example.com", "yourdomain", "sentry.io", "wixpress", "@2x.png", "domain.com")


def deobfuscate(text):
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


# ---------------------------------------------------------------- pages

def fetch_page(url, max_chars=7000):
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer"]):
            tag.decompose()

        links = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("mailto:") or "linkedin.com/in/" in href:
                links.append(href.replace("mailto:", ""))

        body = " ".join(soup.get_text(" ").split())[:max_chars]
        return deobfuscate(body + " " + " ".join(links[:40]))
    except Exception:
        return ""


def ddg_search(query, region, max_results=5, log=print):
    try:
        return list(DDGS().text(query, region=region, max_results=max_results))
    except TypeError:
        return list(DDGS().text(query, max_results=max_results))
    except Exception as e:
        log(f"   search failed: {e}")
        return []


# ---------------------------------------------------------------- prompts

def plan_queries(target, place):
    where = f" located in {place}" if place else ""
    prompt = f"""I am looking for the contact details of a PERSON (or people) matching this description:
"{target}"{where}

Write 4 web search queries that would surface their name, work email, phone number, or LinkedIn profile.
{"Every query must restrict results to " + place + ". Use the country name, or well-known cities in it, inside the query." if place else ""}
Make the queries different from each other. Useful angles include:
- a site:linkedin.com/in query
- the company's team / leadership / staff directory page
- conference speaker bios, press releases, or published papers
- an email pattern query using the company domain

Reply with ONLY a JSON array of 4 strings. No markdown, no explanation."""

    suffix = f" {place}" if place else ""
    fallback = [
        f'site:linkedin.com/in "{target}"{suffix}',
        f'"{target}"{suffix} email contact',
        f'"{target}"{suffix} team OR leadership OR staff directory',
        f'"{target}"{suffix} phone',
    ]

    out = parse_json(ask_llm(prompt), fallback=fallback)
    if not isinstance(out, list) or not out:
        return fallback
    queries = [q for q in out if isinstance(q, str) and q.strip()]
    return queries or fallback


def extract_people(target, url, page_text, hints, place):
    location_rule = ""
    if place:
        location_rule = f"""
LOCATION FILTER — this matters:
- Only include people who are based in {place}.
- Judge this from evidence on the page: an office address, a city, a phone
  country code, or a stated region.
- If the page clearly places someone in a DIFFERENT country, leave them out entirely.
- If the page gives no location at all, you may include them but set confidence to "low"
  and write "location unconfirmed" in notes.
"""

    prompt = f"""Below is text from the web page {url}.

--- PAGE TEXT ---
{page_text}
--- END OF PAGE TEXT ---

Email addresses found in that text by a pattern matcher: {hints['emails'] or 'none'}
Phone numbers found: {hints['phones'] or 'none'}
LinkedIn profiles found: {hints['linkedins'] or 'none'}

I am researching: {target}
{location_rule}
List every PERSON on this page who is relevant to that search and for whom the page gives
at least one of: an email, a phone number, or a LinkedIn profile.

Reply with ONLY this JSON and nothing else:
{{"people": [
  {{"name": "", "role": "", "company": "", "location": "", "email": "", "phone": "", "linkedin": "", "confidence": "high", "notes": ""}}
]}}

Rules:
- One object per person. Return an empty list if nobody on the page qualifies.
- Use "" for any detail the page does not give.
- location: the city and/or country the page gives for this person, or "" if none.
- NEVER construct or guess an email address. Only copy one that appears in the text above.
- Do not include generic mailboxes such as info@, sales@, support@, hello@, careers@, privacy@.
- confidence: "high" if the page clearly ties the detail to that named person,
  "medium" if it is nearby but not explicit, "low" if you are unsure.
- notes: one short phrase on where the detail came from, e.g. "listed on team page"."""

    data = parse_json(ask_llm(prompt), fallback={"people": []})
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        people = data.get("people", [])
        return people if isinstance(people, list) else []
    return []


GENERIC_PREFIXES = ("info@", "sales@", "support@", "hello@", "contact@", "admin@",
                    "careers@", "jobs@", "press@", "privacy@", "legal@", "noreply@", "no-reply@")

OTHER_COUNTRY_WORDS = {name.lower() for _, (name, _) in COUNTRIES.items()} - {"worldwide (no country filter)"}


def _text(person, key):
    v = person.get(key)
    return v.strip() if isinstance(v, str) else ""


def validate(person, page_text, place):
    """Throw away anything the model made up or mislocated. Returns cleaned person, or None."""
    lower_page = page_text.lower()

    for key in ("name", "role", "company", "email", "phone", "linkedin",
                "confidence", "notes", "location"):
        person[key] = _text(person, key)

    email = person["email"]
    if email:
        if email.lower() not in lower_page or email.lower().startswith(GENERIC_PREFIXES):
            person["email"] = ""
            person["notes"] = (person["notes"] + " [email discarded]").strip()

    person["linkedin"] = normalize_linkedin(person["linkedin"])

    loc = person.pop("location", "")
    if loc:
        person["notes"] = (f"{loc} · " + person["notes"]).strip(" ·")

    if place and loc:
        loc_l = loc.lower()
        if place.lower() not in loc_l:
            named_others = [c for c in OTHER_COUNTRY_WORDS
                            if c != place.lower() and c in loc_l]
            if named_others:
                return None

    if not person["name"]:
        return None
    if not (person["email"] or person["phone"] or person["linkedin"]):
        return None
    return person


# ---------------------------------------------------------------- the run

def run_agent(target, country_code="", log=print):
    place = country_name(country_code) if country_code else ""
    region = country_region(country_code)
    search_date = datetime.now().strftime("%Y-%m-%d")

    log(f"Planning searches for: {target}")
    log(f"Country filter: {place or 'none (worldwide)'}  [region {region}]")

    queries = plan_queries(target, place)
    for q in queries:
        log(f"   plan: {q}")

    existing = read_rows()
    seen_urls = {r["source_url"] for r in existing if r["target"] == target}
    seen_people = {(r["name"].strip().lower(), r["email"].strip().lower()) for r in existing}

    findings = []
    pages_read = 0
    dropped_location = 0

    for q in queries:
        if pages_read >= MAX_PAGES:
            break
        log(f"Searching: {q}")
        hits = ddg_search(q, region, max_results=5, log=log)

        for hit in hits:
            if pages_read >= MAX_PAGES:
                log("   page limit reached, stopping")
                break

            url = (hit.get("href") or "").strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

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

            for person in extract_people(target, url, text, hints, place):
                if not isinstance(person, dict):
                    continue
                cleaned = validate(person, text, place)
                if not cleaned:
                    if person.get("name"):
                        dropped_location += 1
                    continue
                person = cleaned

                key = (person["name"].lower(), person["email"].lower())
                if key in seen_people:
                    continue
                seen_people.add(key)

                person["row_id"] = uuid.uuid4().hex[:12]
                person["contacted"] = "no"
                person["comment"] = ""
                person["search_date"] = search_date
                person["target"] = target
                person["country"] = place
                person["source_url"] = url
                person["found_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                findings.append(person)
                log(f"      {person['name']} — {person['email'] or person['phone'] or person['linkedin']}")

            time.sleep(PAUSE_SECONDS)

    findings.reverse()
    append_rows(findings)

    extra = f", {dropped_location} rejected" if dropped_location else ""
    log(f"Finished. Read {pages_read} pages, saved {len(findings)} new people{extra}.")
    return findings


if __name__ == "__main__":
    run_agent(input("Who are you looking for? "),
              input("Country code (e.g. US, blank for all): ").strip().upper())