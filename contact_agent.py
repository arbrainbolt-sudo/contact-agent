"""Searches the web for contact information and writes it to results.csv"""

import os
import csv
import json
import time

import requests
from bs4 import BeautifulSoup
from ddgs import DDGS
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.getenv("OPENROUTER_API_KEY")
MODEL = "openrouter/free"


def ask_llm(prompt):
    """Send one question to the LLM and return its text answer."""
    r = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={
            "model": MODEL,
            "messages": [
                {"role": "system", "content": "You are a careful research assistant. You never invent facts."},
                {"role": "user", "content": prompt},
            ],
        },
        timeout=120,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def parse_json(raw, fallback):
    """LLMs often wrap JSON in ```code fences```. Strip them and parse."""
    text = raw.strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        print("   (model reply wasn't valid JSON — skipping)")
        return fallback


def plan_queries(target):
    """Ask the LLM what to search for."""
    prompt = (
        f"I need contact information for: {target}\n\n"
        "Write 3 different web search queries likely to find it.\n"
        "Reply with ONLY a JSON array of 3 strings. No markdown, no explanation."
    )
    return parse_json(
        ask_llm(prompt),
        fallback=[f"{target} email", f"{target} contact", f"{target} linkedin"],
    )


def fetch_page(url, max_chars=5000):
    """Download a page and return its readable text."""
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer"]):
            tag.decompose()
        return " ".join(soup.get_text(" ").split())[:max_chars]
    except Exception as e:
        print(f"   could not read page: {e}")
        return ""


def extract_contacts(target, url, page_text):
    """Ask the LLM to pull contact details out of one page."""
    prompt = f"""Below is the text of the web page {url}.

--- PAGE TEXT ---
{page_text}
--- END OF PAGE TEXT ---

I am looking for contact information for: {target}

Reply with ONLY this JSON object and nothing else:
{{"name": "", "role": "", "email": "", "phone": "", "linkedin": "", "confidence": "high", "notes": ""}}

Rules:
- Use "" (empty string) for anything the page does not contain.
- confidence must be high, medium, or low.
- Never guess or construct an email address. Only report one if it literally appears in the text."""
    return parse_json(ask_llm(prompt), fallback={})


def main():
    target = input("Who or what do you need contact info for?\n> ")

    queries = plan_queries(target)
    print(f"\nSearch plan: {queries}\n")

    seen = set()
    findings = []

    for q in queries:
        print(f"Searching: {q}")
        for hit in DDGS().text(q, max_results=4):
            url = hit.get("href")
            if not url or url in seen:
                continue
            seen.add(url)

            print(f"   reading {url}")
            text = fetch_page(url)
            if len(text) < 200:
                continue

            info = extract_contacts(target, url, text)
            if info.get("email") or info.get("phone") or info.get("linkedin"):
                info["source_url"] = url
                findings.append(info)
                print(f"      HIT: {info.get('email') or info.get('phone') or info.get('linkedin')}")

            time.sleep(2)   # be polite to websites, stay under rate limits

    if not findings:
        print("\nNothing found. Try a more specific target.")
        return

    fields = ["name", "role", "email", "phone", "linkedin", "confidence", "notes", "source_url"]
    with open("results.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in findings:
            writer.writerow({k: row.get(k, "") for k in fields})

    print(f"\nDone — {len(findings)} rows written to results.csv")


if __name__ == "__main__":
    main()