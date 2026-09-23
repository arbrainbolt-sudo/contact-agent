"""Reads CRM activity notes and suggests the next action for each row."""

import hashlib
from datetime import datetime

import store
from contact_agent import ask_llm, parse_json

SUGGESTION_FIELDS = ["sheet", "row", "who", "org", "activity_hash",
                     "action", "priority", "timing", "analyzed_at"]

PRIORITIES = ("high", "medium", "low")
MAX_ROWS_PER_CALL = 25


def activity_hash(text):
    return hashlib.sha1((text or "").strip().encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------- storage

def load(sheet):
    """Suggestions for one worksheet, keyed by Excel row number."""
    rows = store.read_sheet(store.CRM_SUGGESTIONS_SHEET, SUGGESTION_FIELDS)
    out = {}
    for r in rows:
        if r.get("sheet") != sheet:
            continue
        try:
            out[int(r["row"])] = r
        except (ValueError, TypeError):
            continue
    return out


def save(sheet, new_rows):
    """Replace this sheet's suggestions, leaving other sheets' alone."""
    existing = store.read_sheet(store.CRM_SUGGESTIONS_SHEET, SUGGESTION_FIELDS)
    kept = [r for r in existing if r.get("sheet") != sheet]
    store.write_sheet(store.CRM_SUGGESTIONS_SHEET, SUGGESTION_FIELDS, kept + new_rows)


# ---------------------------------------------------------------- analysis

def _chunk(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def analyze(sheet, rows, cols, log=print):
    """One LLM call per batch of rows. cols maps role -> column header."""
    name_col = cols.get("name", "")
    company_col = cols.get("company", "")
    activity_col = cols.get("activity", "")
    last_col = cols.get("last", "")

    if not activity_col:
        log("   no activity column found — nothing to analyse")
        return []

    candidates = []
    for row in rows:
        activity = (row.get(activity_col, "") or "").strip()
        if not activity:
            continue
        candidates.append({
            "row": row["_row"],
            "who": (row.get(name_col, "") if name_col else "").strip(),
            "org": (row.get(company_col, "") if company_col else "").strip(),
            "last": (row.get(last_col, "") if last_col else "").strip(),
            "activity": activity,
        })

    if not candidates:
        log("   no rows have activity notes yet")
        return []

    log(f"   analysing {len(candidates)} rows with activity notes")
    today = datetime.now().strftime("%Y-%m-%d")
    results = []

    for batch in _chunk(candidates, MAX_ROWS_PER_CALL):
        listing = []
        for c in batch:
            who = c["who"] or "(unnamed)"
            org = f" at {c['org']}" if c["org"] else ""
            last = f" | last contact: {c['last']}" if c["last"] else ""
            listing.append(
                f"[{c['row']}] {who}{org}{last}\nACTIVITY: {c['activity'][:900]}"
            )

        prompt = f"""Today is {today}. Below are notes from a sales CRM.
Each entry starts with a row number in square brackets.

{chr(10).join(listing)}

For EACH row, read the activity notes and say what the single most useful
next action would be.

Reply with ONLY a JSON object mapping each row number to an object:
{{"12": {{"action": "", "priority": "high", "timing": ""}}}}

Rules:
- action: one short imperative sentence, under 15 words. Be specific to what
  the notes actually say — name the thing to send, ask or confirm.
- priority: "high" if something was promised, a deadline is near, or the deal
  is stalling; "medium" for normal progression; "low" for nurture or no urgency.
- timing: a short phrase like "this week", "overdue", "after their board meeting",
  or "" if the notes give no timing.
- Base everything on the notes. Do not invent facts, names or dates.
- Include every row number listed above, exactly once."""

        parsed = parse_json(ask_llm(prompt), fallback={})
        if not isinstance(parsed, dict):
            parsed = {}

        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        for c in batch:
            raw = parsed.get(str(c["row"])) or parsed.get(c["row"]) or {}
            if not isinstance(raw, dict):
                raw = {}

            action = (raw.get("action") or "").strip()
            if not action:
                action = "Review the notes and decide a next step."

            priority = (raw.get("priority") or "").strip().lower()
            if priority not in PRIORITIES:
                priority = "medium"

            results.append({
                "sheet": sheet,
                "row": str(c["row"]),
                "who": c["who"],
                "org": c["org"],
                "activity_hash": activity_hash(c["activity"]),
                "action": action,
                "priority": priority,
                "timing": (raw.get("timing") or "").strip(),
                "analyzed_at": stamp,
            })

    save(sheet, results)
    log(f"Finished. {len(results)} suggestions saved.")
    return results


def merge(rows, cols, sheet):
    """Attach saved suggestions to rows, flagging any that are out of date."""
    stored = load(sheet)
    activity_col = cols.get("activity", "")
    out = []

    for row in rows:
        s = stored.get(row["_row"])
        if not s:
            continue
        activity = (row.get(activity_col, "") if activity_col else "").strip()
        out.append({
            "row": row["_row"],
            "who": s.get("who") or "(unnamed)",
            "org": s.get("org", ""),
            "action": s.get("action", ""),
            "priority": s.get("priority", "medium"),
            "timing": s.get("timing", ""),
            "analyzed_at": s.get("analyzed_at", ""),
            "stale": bool(activity) and activity_hash(activity) != s.get("activity_hash", ""),
        })

    order = {"high": 0, "medium": 1, "low": 2}
    out.sort(key=lambda x: (order.get(x["priority"], 1), x["who"].lower()))
    return out