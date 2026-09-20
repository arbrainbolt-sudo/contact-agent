"""Fetches recent AI news, summarises it, and remembers what you saved."""

import time
import hashlib
from datetime import datetime

from ddgs import DDGS

import store
from contact_agent import ask_llm, parse_json, fetch_page

NEWS_FIELDS = ["news_id", "title", "summary", "url", "source",
               "published", "fetched_at", "saved_at"]

QUERIES = [
    "artificial intelligence",
    "AI models release",
    "AI industry funding",
]

MAX_ITEMS = 9


def _news_id(url):
    return hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------- storage

def latest():
    return store.read_sheet(store.LATEST_NEWS_SHEET, NEWS_FIELDS)


def saved():
    return store.read_sheet(store.SAVED_NEWS_SHEET, NEWS_FIELDS)


def saved_ids():
    return {r["news_id"] for r in saved()}


def save_item(news_id):
    item = next((r for r in latest() if r["news_id"] == news_id), None)
    if not item:
        return False
    rows = saved()
    if any(r["news_id"] == news_id for r in rows):
        return False
    item = dict(item)
    item["saved_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows.insert(0, item)
    store.write_sheet(store.SAVED_NEWS_SHEET, NEWS_FIELDS, rows)
    return True


def unsave_item(news_id):
    rows = saved()
    kept = [r for r in rows if r["news_id"] != news_id]
    if len(kept) == len(rows):
        return False
    store.write_sheet(store.SAVED_NEWS_SHEET, NEWS_FIELDS, kept)
    return True


# ---------------------------------------------------------------- fetching

def _search_news(query, log):
    try:
        return list(DDGS().news(query, region="us-en", max_results=6))
    except TypeError:
        try:
            return list(DDGS().news(query, max_results=6))
        except Exception as e:
            log(f"   news search failed: {e}")
            return []
    except Exception as e:
        log(f"   news search failed: {e}")
        return []


def _summarise(title, snippet, url, log):
    """Four sentences. Falls back to the snippet if the model gives nothing."""
    body = snippet or ""
    if len(body) < 400:
        page = fetch_page(url, max_chars=4000)
        if len(page) > len(body):
            body = page

    prompt = f"""Summarise this news article in EXACTLY four sentences.

TITLE: {title}

TEXT:
{body[:4000]}

Rules:
- Exactly four sentences, plain prose, no bullet points, no preamble.
- Say what happened, who is involved, and why it matters.
- Only use facts from the text above. Do not speculate.

Reply with ONLY the four sentences."""

    out = ask_llm(prompt)
    out = (out or "").strip()
    if not out:
        log(f"   no summary for: {title[:50]}")
        return (body[:400] + "...") if body else "(no summary available)"
    return " ".join(out.split())


def run_news(log=print):
    """Fetch, summarise, store. Returns the list of items."""
    log("Fetching AI news...")

    seen_urls = set()
    raw = []

    for q in QUERIES:
        log(f"   searching: {q}")
        for hit in _search_news(q, log):
            url = (hit.get("url") or hit.get("href") or "").strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            raw.append({
                "url": url,
                "title": (hit.get("title") or "").strip(),
                "snippet": (hit.get("body") or "").strip(),
                "source": (hit.get("source") or "").strip(),
                "published": (hit.get("date") or "").strip(),
            })
        time.sleep(1)

    raw = [r for r in raw if r["title"]][:MAX_ITEMS]
    log(f"   {len(raw)} articles found, summarising...")

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    items = []
    for r in raw:
        log(f"   summarising: {r['title'][:60]}")
        items.append({
            "news_id": _news_id(r["url"]),
            "title": r["title"],
            "summary": _summarise(r["title"], r["snippet"], r["url"], log),
            "url": r["url"],
            "source": r["source"],
            "published": r["published"],
            "fetched_at": now,
            "saved_at": "",
        })

    store.write_sheet(store.LATEST_NEWS_SHEET, NEWS_FIELDS, items)
    log(f"Done. {len(items)} stories ready.")
    return items