"""Events page — five boxes, live discovery, daily auto-refresh.

Boxes:
    5. My Interest        (anything ticked, pulled to the top)
    1. Philadelphia
    2. NY / NJ / DE
    3. All over USA
    4. Large tech companies   (sorted by what's coming up next)

Data lives in an "Events" worksheet inside the same results.xlsx that
store.py manages.

Wiring — two lines in app.py:

    from events import bp as events_bp          # near the other imports
    app.register_blueprint(events_bp)           # after app = Flask(__name__)
"""

import os
import re
import json
import time
import uuid
import html
import threading
from datetime import datetime, date, timedelta

import requests
from flask import (Blueprint, request, redirect, url_for,
                   render_template_string, current_app)
from openpyxl import Workbook, load_workbook

import store

bp = Blueprint("events", __name__)

SHEET = "Events"

# Header-driven read/write, so adding a column later won't break old sheets.
HEADERS = [
    "event_id", "category", "date", "sort_date", "company", "event",
    "location", "meant_for", "register_url", "interested", "source", "notes",
]

CATEGORIES = [
    ("philadelphia", "1. Philadelphia"),
    ("tristate", "2. New York, New Jersey, Delaware"),
    ("usa", "3. All over USA"),
    ("bigtech", "4. Large tech companies"),
]
CATEGORY_LABELS = dict(CATEGORIES)

MEANT_FOR_SUGGESTIONS = [
    "Hackathon / build night", "Tech talk + Q&A", "Hands-on workshop",
    "Networking meetup", "Career meetup", "User group",
    "Open-source community", "Tech roundtable", "Founders / investors",
    "Fintech", "Startup / entrepreneurship",
]

# Only keep discovered events whose title or description mentions one of these.
INTEREST_KEYWORDS = [
    "ai", "artificial intelligence", "machine learning", "llm", "genai", "agent",
    "startup", "founder", "entrepreneur", "venture", "vc", "pitch", "demo day",
    "fintech", "finance", "payments", "blockchain",
    "tech", "developer", "engineering", "software", "data", "cloud", "devops",
    "hackathon", "workshop", "meetup", "networking", "python", "kubernetes",
    "aws", "azure", "salesforce", "databricks", "open source", "career", "hiring",
]

REFRESH_HOUR = 6          # 6am
REFRESH_TZ = "America/New_York"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

_lock = threading.Lock()

# Last-run report, shown on the page.
STATUS = {"when": None, "sources": [], "added": 0, "running": False}


# --------------------------------------------------------------- seed data
#
# (company, event, typical month 1-12 or None, usual location, focus, url)
# Months drive the "what's next" ordering. Exact dates mostly aren't published
# a year out, so these are typical timings — overwrite them on the page as
# each conference confirms.

BIGTECH_SEED = [
    ("NVIDIA", "NVIDIA GTC", 3, "San Jose, CA", "AI, GPUs, robotics, accelerated computing", "https://www.nvidia.com/gtc/"),
    ("Adobe", "Adobe Summit", 3, "Las Vegas, NV", "Digital experience, martech, AI", "https://summit.adobe.com/"),
    ("Google Cloud", "Google Cloud Next", 4, "Las Vegas, NV", "Cloud, Gemini, data, enterprise AI", "https://cloud.withgoogle.com/next"),
    ("Atlassian", "Team", 4, "Anaheim / Las Vegas", "Jira, Confluence, dev productivity, AI", "https://www.atlassian.com/team"),
    ("Google", "Google I/O", 5, "Mountain View, CA + online", "Android, Gemini/AI, Chrome, dev platforms", "https://io.google/"),
    ("Microsoft", "Microsoft Build", 5, "Seattle, WA + online", "Azure, Windows, Copilot, AI agents", "https://build.microsoft.com/"),
    ("SAP", "SAP Sapphire", 5, "Orlando, FL", "ERP, business AI, enterprise transformation", "https://www.sap.com/events/sapphire.html"),
    ("IBM", "IBM Think", 5, "USA (varies)", "Enterprise AI, hybrid cloud, strategy", "https://www.ibm.com/events/"),
    ("ServiceNow", "Knowledge", 5, "Las Vegas, NV", "Workflows, enterprise automation, AI agents", "https://www.servicenow.com/events/knowledge.html"),
    ("Red Hat", "Red Hat Summit", 5, "USA (varies)", "Linux, OpenShift, Kubernetes, hybrid cloud", "https://www.redhat.com/en/summit"),
    ("Dell Technologies", "Dell Technologies World", 5, "Las Vegas, NV", "Infrastructure, storage, AI, enterprise IT", "https://www.dell.com/en-us/dt/events/"),
    ("Apple", "WWDC", 6, "Cupertino, CA + online", "iOS, macOS, Swift, Apple Intelligence", "https://developer.apple.com/wwdc/"),
    ("Cisco", "Cisco Live US", 6, "USA (varies)", "Networking, security, AI, observability", "https://www.ciscolive.com/"),
    ("Snowflake", "Snowflake Summit", 6, "San Francisco, CA", "Data cloud, analytics, AI", "https://www.snowflake.com/summit/"),
    ("Databricks", "Data + AI Summit", 6, "San Francisco, CA", "Lakehouse, data engineering, GenAI, ML", "https://www.databricks.com/dataaisummit"),
    ("AWS", "AWS Summit New York", 7, "New York, NY", "Regional AWS cloud/developer event", "https://aws.amazon.com/events/summits/"),
    ("Broadcom / VMware", "VMware Explore", 8, "Las Vegas, NV", "Virtualisation, private cloud, infrastructure", "https://www.vmware.com/explore"),
    ("Meta", "Meta Connect", 9, "Menlo Park, CA", "AI, smart glasses, VR/AR, Horizon", "https://www.meta.com/connect/"),
    ("Splunk", ".conf", 9, "USA (varies)", "Observability, security, SIEM, IT ops", "https://conf.splunk.com/"),
    ("GitHub", "GitHub Universe", 10, "San Francisco, CA", "Copilot, AI coding, DevOps", "https://githubuniverse.com/"),
    ("Salesforce", "Dreamforce", 10, "San Francisco, CA", "Agentforce/AI, CRM, enterprise apps", "https://www.salesforce.com/dreamforce/"),
    ("Oracle", "Oracle AI World", 10, "Las Vegas, NV", "Oracle Cloud, databases, enterprise AI", "https://www.oracle.com/events/"),
    ("IBM", "IBM TechXchange", 10, "Las Vegas, NV", "Technical / developer-focused IBM event", "https://www.ibm.com/events/"),
    ("Adobe", "Adobe MAX", 10, "Los Angeles, CA", "Creative tech, generative AI, Firefly", "https://max.adobe.com/"),
    ("Samsung", "Samsung Developer Conference", 10, "San Jose, CA", "Mobile, SmartThings, devices, AI", "https://developer.samsung.com/sdc"),
    ("OpenAI", "OpenAI DevDay", 10, "San Francisco, CA", "Models, APIs, agents, developer tools", "https://openai.com/devday/"),
    ("Qualcomm", "Snapdragon Summit", 10, "Maui, HI", "Mobile chips, PCs, edge AI", "https://www.qualcomm.com/snapdragon/summit"),
    ("Cisco", "WebexOne", 11, "USA (varies)", "Collaboration, contact centre, AI comms", "https://www.webexone.com/"),
    ("Microsoft", "Microsoft Ignite", 11, "USA (varies)", "Enterprise IT, Azure, security, M365", "https://ignite.microsoft.com/"),
    ("AWS", "AWS re:Invent", 12, "Las Vegas, NV", "Cloud, AI, databases, serverless", "https://reinvent.awsevents.com/"),
    ("Intel", "Intel developer events", None, "USA (varies)", "CPUs, AI, edge, developer technology", "https://www.intel.com/content/www/us/en/events/"),
    ("AMD", "Advancing AI", None, "USA (varies)", "CPUs, GPUs, AI infrastructure", "https://www.amd.com/en/corporate/events.html"),
]

MONTHS = ["", "January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December"]


def _next_occurrence(month):
    """The next time a month rolls around, as an ISO date for sorting."""
    if not month:
        return ""
    today = date.today()
    year = today.year if month >= today.month else today.year + 1
    return f"{year}-{month:02d}-01"


# ------------------------------------------------------------------ storage

def _backup():
    try:
        fn = getattr(store, "backup_now", None) or getattr(store, "_backup", None)
        if fn:
            fn()
    except Exception as exc:
        print(f"[events] backup skipped: {exc}")


def _workbook():
    if os.path.exists(store.XLSX_FILE):
        return load_workbook(store.XLSX_FILE)
    wb = Workbook()
    wb.remove(wb.active)
    return wb


def _text(v):
    return "" if v is None else str(v).replace("_x000D_", "").strip()


def _seed_rows():
    rows = []
    for company, event, month, where, focus, url in BIGTECH_SEED:
        when = f"Typically {MONTHS[month]}" if month else "Date varies"
        rows.append({
            "event_id": uuid.uuid4().hex[:8], "category": "bigtech",
            "date": when, "sort_date": _next_occurrence(month),
            "company": company, "event": event, "location": where,
            "meant_for": focus, "register_url": url,
            "interested": False, "source": "seed", "notes": "",
        })
    return rows


def _read_sheet(ws):
    """Read rows using the sheet's own header row, so old sheets still load."""
    rows = []
    header = [_text(c) for c in next(ws.iter_rows(min_row=1, max_row=1, values_only=True), [])]
    if not header:
        return rows
    for raw in ws.iter_rows(min_row=2, values_only=True):
        if not raw or not any(raw):
            continue
        row = {k: "" for k in HEADERS}
        row.update({k: _text(v) for k, v in zip(header, raw) if k in HEADERS})
        row["interested"] = str(row["interested"]).lower() in ("1", "yes", "true", "x")
        if not row["event_id"]:
            row["event_id"] = uuid.uuid4().hex[:8]
        rows.append(row)
    return rows


def all_events():
    with _lock:
        wb = _workbook()
        if SHEET not in wb.sheetnames:
            rows = _seed_rows()
            _write_rows(wb, rows)
            _backup()
            wb.save(store.XLSX_FILE)
            return rows
        return _read_sheet(wb[SHEET])


def _write_rows(wb, rows):
    if SHEET in wb.sheetnames:
        wb.remove(wb[SHEET])
    ws = wb.create_sheet(SHEET)
    ws.append(HEADERS)
    for row in rows:
        ws.append([
            row.get("event_id", ""), row.get("category", ""), row.get("date", ""),
            row.get("sort_date", ""), row.get("company", ""), row.get("event", ""),
            row.get("location", ""), row.get("meant_for", ""),
            row.get("register_url", ""), "yes" if row.get("interested") else "",
            row.get("source", ""), row.get("notes", ""),
        ])
    if not wb.sheetnames:
        wb.create_sheet("Sheet1")


def save_all(rows):
    with _lock:
        _backup()
        wb = _workbook()
        _write_rows(wb, rows)
        wb.save(store.XLSX_FILE)


def add_event(data):
    rows = all_events()
    rows.append({
        "event_id": uuid.uuid4().hex[:8],
        "category": data.get("category", "philadelphia"),
        "date": data.get("date", ""),
        "sort_date": _parse_date(data.get("date", "")),
        "company": data.get("company", ""), "event": data.get("event", ""),
        "location": data.get("location", ""), "meant_for": data.get("meant_for", ""),
        "register_url": data.get("register_url", ""),
        "interested": False, "source": "manual", "notes": data.get("notes", ""),
    })
    save_all(rows)


def set_interest(event_id, wanted):
    rows = all_events()
    for row in rows:
        if row["event_id"] == event_id:
            row["interested"] = wanted
    save_all(rows)


def delete_event(event_id):
    save_all([r for r in all_events() if r["event_id"] != event_id])


def _parse_date(text):
    """Best-effort ISO date out of free text. Empty string if we can't tell."""
    text = (text or "").strip()
    for fmt in ("%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y",
                "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(text.replace(",", ""), fmt).date().isoformat()
        except ValueError:
            continue
    return ""


# ---------------------------------------------------------------- discovery

def _classify(location):
    """Decide which box a discovered event belongs in."""
    loc = (location or "").lower()
    if any(w in loc for w in ("philadelphia", "philly", "king of prussia",
                              "conshohocken", "malvern", "wayne, pa", "chester")):
        return "philadelphia"
    if any(w in loc for w in ("new york", "nyc", "brooklyn", "manhattan",
                              "new jersey", ", nj", "newark", "jersey city",
                              "princeton", "delaware", ", de", "wilmington")):
        return "tristate"
    return "usa"


def _interesting(text):
    """Whole-word match. Substring matching turns 'Braiding' into an AI event."""
    low = (text or "").lower()
    return any(re.search(r"\b" + re.escape(k) + r"\b", low) for k in INTEREST_KEYWORDS)


def _event_url(node, base="https://luma.com"):
    """Find the event's own link, whatever the site calls that field."""
    for key in ("url", "slug", "event_url", "permalink", "link", "canonical_url",
                "short_url", "public_url"):
        val = node.get(key)
        if isinstance(val, str) and val.strip():
            val = val.strip()
            if val.startswith("http"):
                return val
            if "/" not in val or val.startswith("/"):
                return f"{base}/{val.lstrip('/')}"
    inner = node.get("event")
    if isinstance(inner, dict):
        return _event_url(inner, base)
    return ""


def _walk_json(node, found):
    """Recursively pull anything that looks like an event out of a JSON blob."""
    if isinstance(node, dict):
        name = node.get("name") or node.get("title")
        start = (node.get("start_at") or node.get("startAt")
                 or node.get("start_date") or node.get("startDate")
                 or node.get("startTime"))
        if isinstance(name, str) and isinstance(start, str) and len(name) > 3:
            found.append(node)
        for value in node.values():
            _walk_json(value, found)
    elif isinstance(node, list):
        for value in node:
            _walk_json(value, found)


def _json_blobs(page_html):
    """Every <script> JSON payload on the page, parsed."""
    blobs = []
    pattern = r'<script[^>]*type="application/(?:ld\+)?json"[^>]*>(.*?)</script>'
    for match in re.findall(pattern, page_html, re.S | re.I):
        try:
            blobs.append(json.loads(match.strip()))
        except Exception:
            pass
    for match in re.findall(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>',
                            page_html, re.S | re.I):
        try:
            blobs.append(json.loads(match.strip()))
        except Exception:
            pass
    return blobs


def _location_of(node):
    loc = node.get("geo_address_info") or node.get("location") or {}
    if isinstance(loc, str):
        return loc
    if isinstance(loc, dict):
        for key in ("full_address", "address", "city_state", "city", "name"):
            val = loc.get(key)
            if isinstance(val, str) and val:
                return val
    return ""


def fetch_luma(city_slug, city_label):
    """Luma city page. Publishes structured JSON and does not block requests."""
    url = f"https://luma.com/{city_slug}"
    page = requests.get(url, headers={"User-Agent": UA}, timeout=25)
    page.raise_for_status()

    found = []
    for blob in _json_blobs(page.text):
        _walk_json(blob, found)

    out = []
    seen = set()
    for node in found:
        name = html.unescape(str(node.get("name") or node.get("title") or "")).strip()
        if not name or name.lower() in seen:
            continue
        start = str(node.get("start_at") or node.get("startAt")
                    or node.get("start_date") or node.get("startDate") or "")
        iso = start[:10] if re.match(r"\d{4}-\d{2}-\d{2}", start) else ""
        location = _location_of(node) or city_label
        if not _interesting(f"{name} {location}"):
            continue
        seen.add(name.lower())
        out.append({
            "date": iso or start[:10], "sort_date": iso,
            "company": "", "event": name, "location": location,
            "meant_for": "", "register_url": _event_url(node), "source": "luma",
        })
    print(f"[events] luma/{city_slug}: {len(found)} objects, {len(out)} kept")
    return out


def fetch_generic(url, source_name, city_label):
    """Try any events page that publishes JSON-LD. Used for Eventbrite/Meetup.

    Both of those actively block automated requests, so this usually returns
    nothing. It is kept because it costs nothing and occasionally works from a
    home IP, and because the same function serves any other site you add.
    """
    page = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "en-US"},
                        timeout=25)
    page.raise_for_status()
    found = []
    for blob in _json_blobs(page.text):
        _walk_json(blob, found)
    out = []
    for node in found:
        name = html.unescape(str(node.get("name") or "")).strip()
        if not name:
            continue
        start = str(node.get("startDate") or node.get("start_date") or "")
        iso = start[:10] if re.match(r"\d{4}-\d{2}-\d{2}", start) else ""
        location = _location_of(node) or city_label
        if not _interesting(f"{name} {location}"):
            continue
        link = _event_url(node, base=re.match(r"https?://[^/]+", url).group(0))
        out.append({
            "date": iso, "sort_date": iso, "company": "", "event": name,
            "location": location, "meant_for": "",
            "register_url": link or url,
            "source": source_name,
        })
    return out


SOURCES = [
    ("Luma Philadelphia", lambda: fetch_luma("philadelphia", "Philadelphia, PA")),
    ("Luma New York",     lambda: fetch_luma("nyc", "New York, NY")),
    ("Eventbrite Philly", lambda: fetch_generic(
        "https://www.eventbrite.com/d/pa--philadelphia/technology--events/",
        "eventbrite", "Philadelphia, PA")),
    ("Meetup Philly",     lambda: fetch_generic(
        "https://www.meetup.com/find/?keywords=technology&location=us--pa--Philadelphia",
        "meetup", "Philadelphia, PA")),
]


def _key(row):
    name = re.sub(r"[^a-z0-9]", "", (row.get("event") or "").lower())
    return (name, (row.get("sort_date") or "")[:10])


def refresh(log=print):
    """Run every source, merge new events in, keep existing ticks intact."""
    if STATUS["running"]:
        return STATUS
    STATUS["running"] = True
    report, discovered = [], []
    try:
        for label, fn in SOURCES:
            try:
                items = fn()
                discovered.extend(items)
                report.append({"name": label, "ok": True, "count": len(items), "note": ""})
                log(f"[events] {label}: {len(items)}")
            except Exception as exc:
                msg = str(exc).split("\n")[0][:80]
                report.append({"name": label, "ok": False, "count": 0, "note": msg})
                log(f"[events] {label} failed: {msg}")

        existing = all_events()
        known = {_key(r) for r in existing}
        added = 0
        for item in discovered:
            if _key(item) in known:
                continue
            known.add(_key(item))
            existing.append({
                "event_id": uuid.uuid4().hex[:8],
                "category": _classify(item["location"]),
                "date": item["date"], "sort_date": item["sort_date"],
                "company": item["company"], "event": item["event"],
                "location": item["location"], "meant_for": item["meant_for"],
                "register_url": item["register_url"], "interested": False,
                "source": item["source"], "notes": "",
            })
            added += 1
        if added:
            save_all(existing)

        STATUS.update({
            "when": datetime.now().strftime("%d %b %Y, %H:%M"),
            "sources": report, "added": added,
        })
        log(f"[events] refresh done — {added} new")
    finally:
        STATUS["running"] = False
    return STATUS


# ---------------------------------------------------------------- scheduler

def _seconds_until_6am():
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(REFRESH_TZ))
    except Exception:
        now = datetime.now()
    target = now.replace(hour=REFRESH_HOUR, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max(60, (target - now).total_seconds())


def _scheduler():
    while True:
        time.sleep(_seconds_until_6am())
        try:
            refresh()
        except Exception as exc:
            print(f"[events] scheduled refresh failed: {exc}")
        time.sleep(90)          # don't re-fire inside the same minute


def start_scheduler():
    """Daily refresh at 6am ET. Skips the Flask reloader's parent process."""
    if os.environ.get("WERKZEUG_RUN_MAIN") == "false":
        return
    thread = threading.Thread(target=_scheduler, daemon=True, name="events-refresh")
    thread.start()
    print(f"[events] daily refresh armed for {REFRESH_HOUR:02d}:00 {REFRESH_TZ}")


start_scheduler()


# ------------------------------------------------------------------- routes

def _logo_exists():
    """Same static/logo.png the Contacts, News and CRM pages use."""
    try:
        return os.path.exists(os.path.join(current_app.static_folder, "logo.png"))
    except Exception:
        return False


def _sort_key(row):
    """Dated events first, soonest to latest; undated last, alphabetical."""
    iso = row.get("sort_date") or ""
    return (0, iso, "") if iso else (1, "", (row.get("event") or "").lower())


@bp.route("/events")
def events_page():
    rows = all_events()
    today = date.today().isoformat()

    for row in rows:
        row["past"] = bool(row.get("sort_date")) and row["sort_date"] < today

    interested = sorted([r for r in rows if r["interested"]], key=_sort_key)
    boxes = []
    for key, label in CATEGORIES:
        subset = [r for r in rows if r["category"] == key and not r["past"]]
        boxes.append((key, label, sorted(subset, key=_sort_key)))

    return render_template_string(
        PAGE, interested=interested, boxes=boxes,
        suggestions=MEANT_FOR_SUGGESTIONS, labels=CATEGORY_LABELS,
        workbook=store.XLSX_FILE, total=len(rows), status=STATUS,
        refresh_hour=REFRESH_HOUR, has_logo=_logo_exists(),
    )


@bp.route("/events/refresh", methods=["POST"])
def events_refresh():
    refresh()
    return redirect(url_for("events.events_page"))


@bp.route("/events/toggle/<event_id>", methods=["POST"])
def events_toggle(event_id):
    set_interest(event_id, request.form.get("interested") == "on")
    return redirect(url_for("events.events_page"))


@bp.route("/events/add", methods=["POST"])
def events_add():
    if request.form.get("event", "").strip() or request.form.get("company", "").strip():
        add_event(request.form)
    return redirect(url_for("events.events_page"))


@bp.route("/events/delete/<event_id>", methods=["POST"])
def events_delete(event_id):
    delete_event(event_id)
    return redirect(url_for("events.events_page"))


# ------------------------------------------------------------------ template

PAGE = """
<!doctype html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Events</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         margin: 0; padding: 20px; background: #f6f7f9; color: #1c1c1c; }
  .wrap { max-width: 1400px; margin: 0 auto; }
  h1 { font-size: 24px; margin: 0 0 4px; color: #1a3a6b; }
  .topbar { display:flex; align-items:center; gap:14px; margin-bottom:18px; }
  .topbar img { height:42px; width:auto; }
  .wordmark { font-size:24px; font-weight:700; letter-spacing:-0.5px; color:#1a3a6b; }
  .sub { font-size: 13px; color: #666; margin-bottom: 4px; }
  nav { margin-bottom: 18px; display: flex; gap: 18px; flex-wrap: wrap; font-size: 15px;
        border-bottom: 1px solid #e0e3e8; padding-bottom: 10px; }
  nav a { color: #1a3a6b; text-decoration: none; }
  nav a.on { font-weight: 700; border-bottom: 2px solid #96C950; padding-bottom: 9px; }

  .bar { display:flex; align-items:center; gap:14px; flex-wrap:wrap; margin: 12px 0 18px; }
  .btn { background: #1a3a6b; color: #fff; border: none; border-radius: 6px;
         padding: 8px 16px; font-size: 13px; cursor: pointer; }
  .btn.ghost { background: #fff; color: #1a3a6b; border: 1px solid #c8d0dd; }
  .srcs { font-size: 12px; color: #777; }
  .ok { color: #3d7a1f; } .bad { color: #a8443a; }

  .box { background: #fff; border: 1px solid #e0e3e8; border-radius: 10px;
         padding: 16px; margin-bottom: 18px; }
  .box h2 { font-size: 15px; margin: 0 0 12px; text-transform: uppercase;
            letter-spacing: .04em; color: #444; }
  .box.mine { border: 2px solid #96C950; background: #fbfdf6; }
  .box.mine h2 { color: #4d7a1f; }
  .grid { display: block; }

  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: .04em;
       color: #888; border-bottom: 1px solid #e6e8ec; padding: 6px 8px; font-weight: 600; }
  td { padding: 8px; border-bottom: 1px solid #f0f1f4; vertical-align: top; }
  tr:last-child td { border-bottom: none; }
  td.co { font-weight: 600; }
  td.dt { white-space: nowrap; color: #555; }
  .soon { color: #b06a00; font-weight: 600; }
  .tag { display: inline-block; font-size: 11px; background: #eef1f6; color: #4a5568;
         border-radius: 4px; padding: 1px 6px; margin-right: 4px; }
  .empty { color: #999; font-size: 13px; padding: 10px 2px; }
  a.reg { color: #1a3a6b; }
  input[type=checkbox] { width: 17px; height: 17px; }
  .del { border: none; background: none; color: #bbb; cursor: pointer; font-size: 15px; }
  .del:hover { color: #c0392b; }

  details { margin-top: 12px; }
  summary { cursor: pointer; font-size: 13px; color: #1a3a6b; }
  .form { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
          gap: 8px; margin-top: 10px; }
  .form input { padding: 7px 9px; border: 1px solid #d5d9e0; border-radius: 6px;
                font-size: 13px; width: 100%; }
  @media (max-width: 760px) { body { padding: 12px; } }
</style>
</head><body><div class="wrap">

<div class="topbar">
  {% if has_logo %}
    <img src="{{ url_for('static', filename='logo.png') }}" alt="Hire2o">
  {% else %}
    <span class="wordmark">Hire2o</span>
  {% endif %}
  <span class="wordmark" style="font-weight:400; color:#555;">Events</span>
</div>

<nav>
  <a href="/">Contacts</a>
  <a href="/news">News</a>
  <a href="/crm">CRM</a>
  <a class="on" href="/events">Events</a>
</nav>

<div class="sub">{{ total }} events &middot; "Events" sheet in {{ workbook }}</div>

<div class="bar">
  <form method="post" action="/events/refresh" style="margin:0">
    <button class="btn" type="submit">&#8635; Refresh now</button>
  </form>
  <span class="srcs">
    Auto-refreshes daily at {{ "%02d"|format(refresh_hour) }}:00 ET.
    {% if status.when %}
      Last run {{ status.when }} &mdash; {{ status.added }} new.
      {% for s in status.sources %}
        <span class="{{ 'ok' if s.ok else 'bad' }}">{{ s.name }}: {{ s.count if s.ok else 'blocked' }}</span>{% if not loop.last %} &middot; {% endif %}
      {% endfor %}
    {% else %}Not run yet this session.{% endif %}
  </span>
</div>

{% macro event_table(rows, show_cat=False, co_label='Organizer') %}
  {% if rows %}
  <table>
    <tr>
      <th style="width:28px"></th><th>Date</th><th>{{ co_label }}</th><th>Event</th>
      <th>Location</th><th>Meant for</th><th>Register</th>
      {% if show_cat %}<th>Where</th>{% endif %}<th></th>
    </tr>
    {% for r in rows %}
    <tr>
      <td>
        <form method="post" action="/events/toggle/{{ r.event_id }}" style="margin:0">
          <input type="checkbox" name="interested" {% if r.interested %}checked{% endif %}
                 onchange="this.form.submit()">
        </form>
      </td>
      <td class="dt {% if r.sort_date %}soon{% endif %}">{{ r.date or "—" }}</td>
      <td class="co">{{ r.company }}</td>
      <td>{{ r.event }}</td>
      <td>{{ r.location }}</td>
      <td>{% if r.meant_for %}<span class="tag">{{ r.meant_for }}</span>{% endif %}</td>
      <td>{% if r.register_url %}<a class="reg" href="{{ r.register_url }}" target="_blank"
            rel="noopener">Register</a>{% endif %}</td>
      {% if show_cat %}<td><span class="tag">{{ labels[r.category] }}</span></td>{% endif %}
      <td>
        <form method="post" action="/events/delete/{{ r.event_id }}" style="margin:0"
              onsubmit="return confirm('Delete this event?')">
          <button class="del" type="submit" title="Delete">&times;</button>
        </form>
      </td>
    </tr>
    {% endfor %}
  </table>
  {% else %}<div class="empty">Nothing here yet.</div>{% endif %}
{% endmacro %}

<div class="box mine">
  <h2>&#9733; My Interest &mdash; {{ interested|length }}</h2>
  {{ event_table(interested, show_cat=True, co_label='Organizer / Company') }}
</div>

<div class="grid">
  {% for key, label, rows in boxes %}
  {% set co_label = 'Company' if key == 'bigtech' else 'Organizer' %}
  <div class="box">
    <h2>{{ label }} &mdash; {{ rows|length }}</h2>
    {{ event_table(rows, co_label=co_label) }}
    <details>
      <summary>+ Add an event</summary>
      <form class="form" method="post" action="/events/add">
        <input type="hidden" name="category" value="{{ key }}">
        <input name="date" placeholder="Date e.g. 2027-05-14">
        <input name="company" placeholder="{{ co_label }}">
        <input name="event" placeholder="Event name">
        <input name="location" placeholder="Location">
        <input name="meant_for" list="meantfor" placeholder="Meant for">
        <input name="register_url" placeholder="Registration link">
        <button class="btn" type="submit">Add</button>
      </form>
    </details>
  </div>
  {% endfor %}
</div>

<datalist id="meantfor">
  {% for s in suggestions %}<option value="{{ s }}">{% endfor %}
</datalist>

</div></body></html>
"""
