"""Fetches recent AI news, summarises it, categorises it, and remembers what you saved."""

import time
import hashlib
from datetime import datetime

from ddgs import DDGS

import store
from contact_agent import ask_llm, parse_json, fetch_page

NEWS_FIELDS = ["news_id", "category", "title", "summary", "url", "source",
               "published", "fetched_at", "saved_at"]

# Display order on the page. Every story lands in exactly one of these.
CATEGORIES = [
    "Research & Models",
    "Products",
    "Agents",
    "Companies",
    "Startups & Funding",
    "Infrastructure & Chips",
    "Enterprise AI",
    "Regulation & Legal",
    "Safety & Security",
    "Industry Applications",
]

CATEGORY_HINTS = {
    "Research & Models":      "new model releases, benchmarks, papers, training techniques, capability results",
    "Products":               "consumer or developer product launches, apps, features, APIs, pricing changes",
    "Agents":                 "autonomous agents, agentic workflows, tool use, computer use, multi-agent systems",
    "Companies":              "corporate news about established AI labs and big tech — leadership, strategy, earnings, partnerships",
    "Startups & Funding":     "funding rounds, valuations, IPOs, acquisitions, new startups launching",
    "Infrastructure & Chips": "GPUs, semiconductors, data centres, energy, cloud capacity, hardware supply",
    "Enterprise AI":          "companies adopting AI internally, workforce impact, productivity, enterprise deployment",
    "Regulation & Legal":     "laws, government policy, lawsuits, copyright disputes, courts, antitrust",
    "Safety & Security":      "AI safety, alignment, misuse, jailbreaks, deepfakes, cyberattacks, breaches",
    "Industry Applications":  "AI applied in a specific sector — healthcare, finance, legal, retail, science, education, defence",
}

DEFAULT_CATEGORY = "Companies"

QUERIES = [
    "artificial intelligence",
    "AI model release benchmark",
    "AI agents autonomous",
    "AI startup funding round",
    "AI chips data center",
    "AI regulation lawsuit",
]

MAX_ITEMS = 12


def _news_id(url):
    return hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------- storage

def latest():
    return store.read_sheet(store.LATEST_NEWS_SHEET, NEWS_FIELDS)


def saved():
    return store.read_sheet(store.SAVED_NEWS_SHEET, NEWS_FIELDS)


def saved_ids():
    return {r["news_id"] for r in saved()}


def find_item(news_id):
    for row in latest():
        if row["news_id"] == news_id:
            return row
    for row in saved():
        if row["news_id"] == news_id:
            return row
    return None


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


def group_by_category(items):
    """[(category, [items])] in display order, empty categories dropped."""
    buckets = {c: [] for c in CATEGORIES}
    for it in items:
        cat = (it.get("category") or "").strip()
        if cat not in buckets:
            cat = classify_fallback(it.get("title", ""), it.get("summary", ""))
        buckets[cat].append(it)
    return [(c, buckets[c]) for c in CATEGORIES if buckets[c]]


# ---------------------------------------------------------------- categorising

# Checked in order — most specific signals first.
KEYWORD_RULES = [
    ("Startups & Funding",     ["series a", "series b", "series c", "series d", "seed round",
                                "funding round", "raises $", "raised $", "valuation", "valued at",
                                "ipo", "acquires", "acquisition", "acquired by", "venture"]),
    ("Regulation & Legal",     ["lawsuit", "sues", "sued", "court", "judge", "copyright",
                                "regulation", "regulator", "ai act", "legislation", "bill",
                                "antitrust", "ftc", "attorney general", "ruling", "ban on",
                                "compliance", "gdpr", "settlement"]),
    ("Safety & Security",      ["safety", "alignment", "jailbreak", "misuse", "deepfake",
                                "breach", "hacked", "cyberattack", "malicious", "red team",
                                "guardrail", "harmful", "abuse", "vulnerability", "phishing"]),
    ("Infrastructure & Chips", ["chip", "gpu", "nvidia", "tsmc", "semiconductor", "data center",
                                "data centre", "wafer", "foundry", "cluster", "compute capacity",
                                "power grid", "energy demand", "hbm", "accelerator"]),
    ("Agents",                 ["agent", "agentic", "autonomous ai", "tool use", "computer use",
                                "multi-agent", "orchestrat", "workflow automation", "mcp"]),
    ("Research & Models",      ["benchmark", "research", "paper", "arxiv", "parameters",
                                "training run", "pretrain", "fine-tun", "reasoning model",
                                "open-weight", "open weights", "state of the art", "outperform",
                                "releases model", "new model"]),
    ("Industry Applications",  ["healthcare", "hospital", "clinical", "diagnos", "drug discovery",
                                "radiolog", "bank", "insurance", "retail", "manufactur",
                                "agricultur", "education", "classroom", "defense", "defence",
                                "military", "legal industry", "logistics"]),
    ("Enterprise AI",          ["enterprise", "workforce", "employees", "productivity", "adoption",
                                "rollout", "deployment", "internal tool", "layoff", "job cuts",
                                "back office", "customer service"]),
    ("Products",               ["launch", "launches", "unveil", "rolls out", "now available",
                                "new feature", "app update", "pricing", "subscription",
                                "api access", "general availability", "beta"]),
    ("Companies",              ["ceo", "hires", "partnership", "earnings", "revenue", "restructur",
                                "openai", "anthropic", "google", "meta", "microsoft", "amazon"]),
]


def classify_fallback(title, summary=""):
    """Deterministic keyword classifier, used when the model gives nothing usable."""
    text = f"{title} {summary}".lower()
    for category, keywords in KEYWORD_RULES:
        if any(k in text for k in keywords):
            return category
    return DEFAULT_CATEGORY


def classify_all(items, log=print):
    """One LLM call to sort every story at once, so near-duplicates land together."""
    if not items:
        return

    menu = "\n".join(f"- {c}: {CATEGORY_HINTS[c]}" for c in CATEGORIES)
    headlines = "\n".join(
        f"{n}. {i['title']} — {i['summary'][:160]}"
        for n, i in enumerate(items, 1)
    )

    prompt = f"""Sort each news story below into EXACTLY ONE category.

CATEGORIES:
{menu}

STORIES:
{headlines}

Rules:
- Every story gets exactly one category. Never assign two.
- Pick the single best fit. If a story could fit several, choose the one matching
  its main point, not a detail it mentions in passing.
- Use the category names exactly as written above.

Reply with ONLY a JSON object mapping each story number to its category, like:
{{"1": "Products", "2": "Regulation & Legal"}}"""

    result = parse_json(ask_llm(prompt), fallback={})
    assigned = 0

    for n, item in enumerate(items, 1):
        chosen = ""
        if isinstance(result, dict):
            raw = result.get(str(n)) or result.get(n)
            if isinstance(raw, str) and raw.strip() in CATEGORIES:
                chosen = raw.strip()

        if chosen:
            assigned += 1
        else:
            chosen = classify_fallback(item["title"], item["summary"])

        item["category"] = chosen

    log(f"   categorised {assigned}/{len(items)} by model, rest by keyword")


# ---------------------------------------------------------------- fetching

def _search_news(query, log, n=5):
    try:
        return list(DDGS().news(query, region="us-en", max_results=n))
    except TypeError:
        try:
            return list(DDGS().news(query, max_results=n))
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
    """Fetch, summarise, categorise, store. Returns the list of items."""
    log("Fetching AI news...")

    seen_urls = set()
    seen_titles = set()
    raw = []

    for q in QUERIES:
        log(f"   searching: {q}")
        for hit in _search_news(q, log):
            url = (hit.get("url") or hit.get("href") or "").strip()
            title = (hit.get("title") or "").strip()
            if not url or not title or url in seen_urls:
                continue
            # crude duplicate-headline guard across queries
            key = title.lower()[:60]
            if key in seen_titles:
                continue
            seen_urls.add(url)
            seen_titles.add(key)
            raw.append({
                "url": url,
                "title": title,
                "snippet": (hit.get("body") or "").strip(),
                "source": (hit.get("source") or "").strip(),
                "published": (hit.get("date") or "").strip(),
            })
        time.sleep(1)

    raw = raw[:MAX_ITEMS]
    log(f"   {len(raw)} articles found, summarising...")

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    items = []
    for r in raw:
        log(f"   summarising: {r['title'][:60]}")
        items.append({
            "news_id": _news_id(r["url"]),
            "category": "",
            "title": r["title"],
            "summary": _summarise(r["title"], r["snippet"], r["url"], log),
            "url": r["url"],
            "source": r["source"],
            "published": r["published"],
            "fetched_at": now,
            "saved_at": "",
        })

    log("   sorting into sections...")
    classify_all(items, log=log)

    store.write_sheet(store.LATEST_NEWS_SHEET, NEWS_FIELDS, items)
    log(f"Done. {len(items)} stories in {len(group_by_category(items))} sections.")
    return items