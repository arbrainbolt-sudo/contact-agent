"""The web app. Run this, then open http://localhost:5000 in your browser."""

import os
import re
import math
import time
import threading
import contextlib
from datetime import datetime, date, timedelta
from urllib.parse import urlparse

from flask import Flask, request, redirect, url_for, render_template_string, jsonify
from events import bp as events_bp
from expenses import bp as expenses_bp
import contact_agent
import crm
import linkedin
import news
import notify
import store
import xcom
import youtube

app = Flask(__name__)
app.register_blueprint(events_bp)
app.register_blueprint(expenses_bp)

state = {"running": False, "target": "", "country": "", "log": []}
news_state = {"running": False, "log": [], "last_run": "", "last_run_ts": 0.0}
crm_state = {"running": False, "log": [], "last_run": "", "sheet": ""}
yt_state = {"running": False, "log": [], "last_run": ""}
x_state = {"running": False, "log": [], "last_run": ""}
li_state = {"running": False, "log": [], "last_run": ""}

NO_COMPANY = "__blank__"
NEWS_TIME = os.getenv("NEWS_TIME", "07:00")                 # daily auto-run, 24h clock
NEWS_COOLDOWN = int(os.getenv("NEWS_COOLDOWN", "300"))      # seconds between manual refreshes
CRM_SHEET = os.getenv("CRM_SHEET", "Sales")                 # default worksheet for the CRM tab
CRM_STALE_DAYS = int(os.getenv("CRM_STALE_DAYS", "30"))     # "needs reconnecting" threshold
CRM_WIDE_COL = os.getenv("CRM_WIDE_COL", "activity")        # wide, editable column
CRM_ACTIVITY_COL = os.getenv("CRM_ACTIVITY_COL", "")        # blank = use CRM_WIDE_COL
CRM_MAX_COLS = int(os.getenv("CRM_MAX_COLS", "6"))          # hide columns 7 onwards
CRM_HIDE_COLS = {1}                    # column numbers never shown on the page
CRM_SNO_COL = 2                        # which column holds the serial number
CRM_COL_LABELS = {2: "S.No."}          # display-only header overrides
CRM_DATE_COL_NUM = int(os.getenv("CRM_DATE_COL_NUM", "6"))       # column F: action date (calendar picker)
CRM_DATE_COL = os.getenv("CRM_DATE_COL", "")                     # or match it by header name instead
CRM_TASK_COL_NUM = int(os.getenv("CRM_TASK_COL_NUM", "7"))       # column G: the action item itself
CRM_TASK_COL = os.getenv("CRM_TASK_COL", "")                     # or match it by header name instead
CRM_PRIORITY_HEADER = os.getenv("CRM_PRIORITY_HEADER", "Priority")   # colour swatch column
# plain editable columns beyond the first few - J, K, L (phone, email, location)
CRM_EXTRA_COLS = tuple(int(n) for n in
                       os.getenv("CRM_EXTRA_COLS", "10,11,12").replace(" ", "").split(",") if n)
CRM_PRIORITY_COL_NUM = int(os.getenv("CRM_PRIORITY_COL_NUM", "9"))   # column I

# click order of the swatch, the word written to the sheet, and sort order
PRIORITY_COLOURS = ("blue", "red", "yellow", "green")
PRIORITY_LABELS = {"blue": "Blue", "red": "Red", "yellow": "Yellow", "green": "Green"}
PRIORITY_MEANING = {"red": "Hot", "yellow": "Warm", "green": "Room Temp", "blue": "Cold"}
PRIORITY_ORDER = {"red": 0, "yellow": 1, "green": 2, "blue": 3}

# both the colour names and the older Hot/Warm wording read back correctly
PRIORITY_LOOKUP = {"hot": "red", "warm": "yellow", "cold": "blue",
                   "room temp": "green", "roomtemp": "green",
                   "room temperature": "green"}
for _colour in PRIORITY_COLOURS:
    PRIORITY_LOOKUP[_colour] = _colour
LINKEDIN_TIME = os.getenv("LINKEDIN_TIME", "07:00")
YOUTUBE_TIME = os.getenv("YOUTUBE_TIME", "07:15")           # daily stats refresh         # daily post, 24h clock


# ---------------------------------------------------------------- helpers

def domain_of(url):
    try:
        host = urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else (host or url)
    except Exception:
        return url


def textarea_rows(text, width=95, min_rows=2, max_rows=24):
    """How many lines this content needs, so the row is exactly tall enough."""
    lines = 0
    for para in (text or "").split("\n"):
        lines += max(1, math.ceil(len(para) / width))
    return max(min_rows, min(max_rows, lines))


app.jinja_env.filters["domain"] = domain_of
app.jinja_env.filters["rows_for"] = textarea_rows


def company_list(rows):
    names = {(r.get("company") or "").strip() for r in rows}
    names.discard("")
    return sorted(names, key=str.lower)


def apply_filters(rows, company, q):
    if company == NO_COMPANY:
        rows = [r for r in rows if not (r.get("company") or "").strip()]
    elif company:
        rows = [r for r in rows if (r.get("company") or "").strip() == company]
    if q:
        needle = q.strip().lower()
        rows = [r for r in rows
                if any(needle in (r.get(f) or "").lower()
                       for f in contact_agent.DISPLAY_FIELDS)]
    return rows


def back(source):
    params = {k: v for k, v in (("company", source.get("company", "")),
                                ("q", source.get("q", ""))) if v}
    return redirect(url_for("home", **params))


def logo_exists():
    return os.path.exists(os.path.join(app.static_folder, "logo.png"))


# ---------------------------------------------------------------- CRM analysis

# US-style month/day first, since that is the likely local convention.
DATE_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y",
                "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y", "%d-%b-%Y",
                "%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y")

NAME_HINTS = ("name", "person", "contact")
COMPANY_HINTS = ("company", "account", "organi", "client")
FOLLOWUP_HINTS = ("follow", "next step", "next action", "action", "task", "to do", "todo", "call")
DUE_HINTS = ("due", "next contact", "follow up date", "followup date", "next date", "reminder")
LASTCONTACT_HINTS = ("last contact", "last contacted", "last touch", "last activity",
                     "last call", "last meeting", "last spoke", "contacted on",
                     "activity date", "last")


def parse_any_date(text):
    """Best-effort date parse from whatever the spreadsheet cell held."""
    t = (text or "").strip()
    if not t:
        return None
    for candidate in (t, t.split(" ")[0]):
        for fmt in DATE_FORMATS:
            try:
                return datetime.strptime(candidate, fmt)
            except ValueError:
                continue
    return None


# Cells that hold a date AND a clock time, e.g. "Nov 12, 2026 3:30 PM".
DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %I:%M %p", "%Y-%m-%d %I:%M%p",
    "%m/%d/%Y %H:%M", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %I:%M%p",
    "%m/%d/%y %H:%M", "%m/%d/%y %I:%M %p", "%m/%d/%y %I:%M%p",
    "%d/%m/%Y %H:%M", "%Y/%m/%d %H:%M",
    "%b %d, %Y %H:%M", "%b %d, %Y %I:%M %p", "%b %d, %Y %I:%M%p",
    "%B %d, %Y %H:%M", "%B %d, %Y %I:%M %p", "%B %d, %Y %I:%M%p",
    "%b %d %Y %I:%M %p", "%B %d %Y %I:%M %p",
    "%d %b %Y %H:%M", "%d %b %Y %I:%M %p",
    "%b %d %I:%M %p", "%b %d %H:%M",
)


def parse_when(text):
    """Parse a 'date and time' cell. Returns (datetime or None, has_time)."""
    t = " ".join((text or "").split()).replace(" at ", " ")
    if not t:
        return None, False

    for fmt in DATETIME_FORMATS:
        try:
            when = datetime.strptime(t, fmt)
        except ValueError:
            continue
        if when.year == 1900:                       # format carried no year
            when = when.replace(year=datetime.now().year)
        return when, True

    when = parse_any_date(t)
    if when:
        return when, bool(when.hour or when.minute)
    return None, False


def pretty_when(when, has_time):
    """'Nov 12, 2026 - 3:30 PM', without zero-padded day or hour."""
    shown = when.strftime("%b %d, %Y")
    if has_time:
        shown += when.strftime(" - %I:%M %p")
    return shown.replace(" 0", " ").replace("- 0", "- ")


def iso_date(text):
    """Whatever the cell holds -> 'YYYY-MM-DD', which is what <input type=date> needs."""
    when, _ = parse_when(text)
    return when.strftime("%Y-%m-%d") if when else ""


def priority_of(text):
    """Cell text ('Hot', 'red', blank...) -> one of the four swatch colours."""
    return PRIORITY_LOOKUP.get((text or "").strip().lower(), "blue")


def priority_label(colour):
    """Swatch colour -> the word written into the spreadsheet."""
    return PRIORITY_LABELS.get(colour, PRIORITY_LABELS["blue"])


def col_letter(number):
    """1 -> A, 8 -> H, 27 -> AA."""
    letters = ""
    while number > 0:
        number, rest = divmod(number - 1, 26)
        letters = chr(65 + rest) + letters
    return letters or "?"


def find_priority_col(headers):
    """Excel column number of an existing Priority column, else 0."""
    wanted = CRM_PRIORITY_HEADER.strip().lower()
    for i, header in enumerate(headers):
        if (header or "").strip().lower() == wanted:
            return i + 1
    return 0


def _store_lock():
    """Reuse store's own write lock when it has one, so direct writes stay safe."""
    for name in ("_LOCK", "LOCK", "_lock", "lock"):
        candidate = getattr(store, name, None)
        if hasattr(candidate, "__enter__"):
            return candidate
    return contextlib.nullcontext()


def write_cells(sheet, cells):
    """Write (row, col, value) triples, check they stuck, and force them through if not.

    store.update_cell is tried first. Some workbooks will not accept a write to a
    column past the last one in use, which fails silently, so anything that does not
    read back gets written straight into the file with openpyxl.
    """
    for excel_row, col, value in cells:
        try:
            store.update_cell(sheet, excel_row, col, value)
        except Exception as exc:
            print(f"update_cell failed for {sheet}!{col_letter(col)}{excel_row}: {exc}")

    missed = [c for c in cells if not cell_reads_back(sheet, *c)]
    if not missed:
        return []

    names = ", ".join(f"{col_letter(c)}{r}" for r, c, _ in missed)
    print(f"{len(missed)} cell(s) did not save through store.update_cell ({names}); "
          f"writing them into {store.XLSX_FILE} directly")
    try:
        import openpyxl
        with _store_lock():
            book = openpyxl.load_workbook(store.XLSX_FILE)
            page = book[sheet]
            for excel_row, col, value in missed:
                page.cell(row=excel_row, column=col).value = value
            book.save(store.XLSX_FILE)
    except Exception as exc:
        print(f"direct write to {store.XLSX_FILE} failed: {exc}")
        return missed

    still = [c for c in missed if not cell_reads_back(sheet, *c)]
    for excel_row, col, value in still:
        print(f"WARNING: {sheet}!{col_letter(col)}{excel_row} still does not hold {value!r}")
    return still


def cell_reads_back(sheet, excel_row, col, value):
    """True when the sheet really holds this value now."""
    headers, rows = store.read_sheet_auto(sheet)
    if excel_row == 1:
        return len(headers) >= col and (headers[col - 1] or "").strip() == value.strip()
    header = headers[col - 1] if len(headers) >= col else ""
    if not header:
        return False
    for row in rows:
        if row.get("_row", 0) == excel_row:
            return (row.get(header, "") or "").strip() == value.strip()
    return False


def priority_column(headers):
    """Where Priority lives: (column number, warning). Column H unless that is taken."""
    found = find_priority_col(headers)
    if found:
        return found, ""

    target = CRM_PRIORITY_COL_NUM
    taken = headers[target - 1].strip() if len(headers) >= target else ""
    if taken:
        spare = len(headers) + 1
        return spare, (f"Column {col_letter(target)} already holds \"{taken}\", so Priority "
                       f"was put in column {col_letter(spare)} instead.")
    return target, ""


app.jinja_env.filters["iso_date"] = iso_date
app.jinja_env.filters["prio"] = priority_of
app.jinja_env.filters["priolabel"] = priority_label
app.jinja_env.filters["priomeaning"] = lambda colour: PRIORITY_MEANING.get(colour, "Cold")


def find_col(headers, hints, override="", exclude=()):
    """Locate a column: exact override, then exact hint, then prefix, then contains."""
    pool = [h for h in headers if h not in exclude]

    if override:
        for h in pool:
            if h.strip().lower() == override.strip().lower():
                return h

    low = [(h, h.strip().lower()) for h in pool]
    for test in (lambda hl, hint: hl == hint,
                 lambda hl, hint: hl.startswith(hint),
                 lambda hl, hint: hint in hl):
        for hint in hints:
            for h, hl in low:
                if test(hl, hint):
                    return h
    return ""
def _sort_key(value):
    """Numbers numerically, text alphabetically, blanks last."""
    v = (value or "").strip()
    if not v:
        return (2, 0.0, "")
    try:
        return (0, float(v.replace(",", "").replace("$", "")), "")
    except ValueError:
        return (1, 0.0, v.lower())

# ----------------------------------------------------------------------------
# Relationship journey: turn one Activity cell into dated, classified touchpoints
# ----------------------------------------------------------------------------

MONTH_SHORT = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
MONTH_LONG = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
              "july": 7, "august": 8, "september": 9, "sept": 9, "october": 10,
              "november": 11, "december": 12}

J_NO_RESPONSE = ("no response", "no answer", "did not receive", "did not respond", "no reply",
                 "not received", "no clue", "missed", "not much", "reminder", "unanswered",
                 "never responded", "still no", "no update", "not reachable", "left message",
                 "left a message", "voicemail", "bounced", "chased", "follow up again")
J_IN_PERSON = ("in person", "met in", " met ", "lunch", "dinner", "coffee", "clubhouse",
               "visited", "conference", "booth", "atl meeting")
J_PROGRESS = ("demo", "onboard", "pricing", "quote", "proposal", "contract", "signed",
              "responded", "feedback", "interested", "meeting set", "set up meeting",
              "will share", "introduce", "resumes", "interviews", "agreed", "confirmed",
              "shared", "sow", "trial", "poc", "referred", "referral")

J_WHATSAPP = ("wa ", " wa", "wa-", "whatsapp", "w/a")
J_CALL = ("call", "spoke", "zoom", "teams", "phone", "dial")
J_MESSAGE = ("txt", "text", "sms", "email", "mail", "message", "msg", "linkedin", "dm ")

J_DATE_PATTERNS = (
    re.compile(r"^\s*([A-Za-z]{3,9})\.?\s+(\d{1,2})\s*(?:-\s*\d{1,2})?\b"),      # Jul 21, Sep 14-16
    re.compile(r"^\s*(\d{1,2})\s*([A-Za-z]{3,9})\b"),                            # 3 jul
    re.compile(r"^\s*([A-Za-z]{3,9})\.?\s+(?:first|second|third|last|early|mid|late)"
               r"(?:\s+week)?(?:\s+of)?\b"),                                     # Nov first week
)

JOURNEY_LEVELS = ("No response", "Contact", "Progress", "In person")


def _month_num(word):
    w = (word or "").strip().lower().rstrip(".")
    return MONTH_LONG.get(w) or MONTH_SHORT.get(w[:3])


def _activity_lines(text):
    text = (text or "").replace("_x000D_", "\n").replace("\r\n", "\n").replace("\r", "\n")
    return [line.strip() for line in text.split("\n")]


def classify_touchpoint(note):
    """A note -> (engagement level, channel)."""
    low = " " + note.lower() + " "
    if any(k in low for k in J_NO_RESPONSE):
        level = "No response"
    elif any(k in low for k in J_IN_PERSON):
        level = "In person"
    elif any(k in low for k in J_PROGRESS):
        level = "Progress"
    else:
        level = "Contact"

    if any(k in low for k in J_IN_PERSON):
        channel = "in person"
    elif any(k in low for k in J_WHATSAPP):
        channel = "WhatsApp"
    elif any(k in low for k in J_CALL):
        channel = "call"
    elif any(k in low for k in J_MESSAGE):
        channel = "message"
    else:
        channel = "message"
    return level, channel


def parse_activity(text, today=None):
    """Activity cell -> touchpoints in date order, each with a level and a channel."""
    today = today or datetime.now().date()
    year, prev_month, points = None, None, []

    for line in _activity_lines(text):
        if not line:
            continue

        only_year = re.fullmatch(r"(20\d{2})\s*[/\\-]?\s*", line)
        if only_year:
            year = int(only_year.group(1))
            continue
        leading_year = re.match(r"^\s*(20\d{2})\s*[/\\]\s*(.+)$", line)
        if leading_year:
            year = int(leading_year.group(1))
            line = leading_year.group(2).strip()

        month = day = None
        for i, pattern in enumerate(J_DATE_PATTERNS):
            found = pattern.match(line)
            if not found:
                continue
            if i == 0:
                month, day = _month_num(found.group(1)), int(found.group(2))
            elif i == 1:
                month, day = _month_num(found.group(2)), int(found.group(1))
            else:
                month, day = _month_num(found.group(1)), 1
            if month:
                line = line[found.end():]
                break
            month = day = None

        if not month:
            continue

        use_year = year or today.year
        if prev_month and month < prev_month - 6:      # the list rolled into a new year
            use_year += 1
            year = use_year
        prev_month = month

        last = 29 if month == 2 else 30 if month in (4, 6, 9, 11) else 31
        day = min(max(day, 1), last)
        note = re.sub(r"^\s*[-:–]\s*", "", line).strip() or "(no note)"
        level, channel = classify_touchpoint(note)

        try:
            when = datetime(use_year, month, day).date()
        except ValueError:
            continue
        points.append({"date": when.isoformat(), "_d": when, "note": note,
                       "level": level, "channel": channel, "future": when > today})

    points.sort(key=lambda p: p["_d"])
    for point in points:
        if point["future"]:
            point["level"] = "Next step"
        point.pop("_d")
    return points


def journey_summary(points):
    levels = [p["level"] for p in points]
    days = None
    if points:
        first = datetime.fromisoformat(points[0]["date"])
        for point in points:
            if point["level"] in ("Progress", "In person"):
                days = (datetime.fromisoformat(point["date"]) - first).days
                break
    return {"touchpoints": len(points),
            "no_response": levels.count("No response"),
            "days": days,
            "progress": levels.count("Progress") + levels.count("In person")}


def journey_months(points):
    """Per-month counts for the bar chart, with no gaps in the run of months."""
    if not points:
        return []
    first = datetime.fromisoformat(points[0]["date"])
    last = datetime.fromisoformat(points[-1]["date"])
    months, cursor = [], datetime(first.year, first.month, 1)
    while cursor <= last:
        months.append({"key": cursor.strftime("%Y-%m"),
                       "label": cursor.strftime("%b"),
                       "year": cursor.year,
                       "counts": {}})
        cursor = datetime(cursor.year + (cursor.month == 12),
                          1 if cursor.month == 12 else cursor.month + 1, 1)
    index = {m["key"]: m for m in months}
    for point in points:
        key = point["date"][:7]
        if key in index:
            counts = index[key]["counts"]
            counts[point["level"]] = counts.get(point["level"], 0) + 1
    return months


def crm_boxes(headers, rows):
    """Build the two summary panels shown above the CRM table."""
    company_col = find_col(headers, COMPANY_HINTS, os.getenv("CRM_COMPANY_COL", ""))
    name_col = find_col(headers, NAME_HINTS, os.getenv("CRM_NAME_COL", ""),
                        exclude=(company_col,))
    followup_col = find_col(headers, FOLLOWUP_HINTS, os.getenv("CRM_FOLLOWUP_COL", ""))
    due_col = find_col(headers, DUE_HINTS, os.getenv("CRM_DUE_COL", ""))
    last_col = find_col(headers, LASTCONTACT_HINTS, os.getenv("CRM_LASTCONTACT_COL", ""))
    activity_col = find_col(headers, (CRM_WIDE_COL,), CRM_ACTIVITY_COL)

    # Column F holds the action date, column G the action item - by position, or by header name.
    date_col = headers[CRM_DATE_COL_NUM - 1] if len(headers) >= CRM_DATE_COL_NUM else ""
    if CRM_DATE_COL:
        date_col = find_col(headers, (), CRM_DATE_COL) or date_col

    task_col = headers[CRM_TASK_COL_NUM - 1] if len(headers) >= CRM_TASK_COL_NUM else ""
    if CRM_TASK_COL:
        task_col = find_col(headers, (), CRM_TASK_COL) or task_col

    prio_num = find_priority_col(headers)
    prio_col = headers[prio_num - 1] if prio_num else ""

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = today - timedelta(days=CRM_STALE_DAYS)

    def label(row):
        who = (row.get(name_col, "") if name_col else "").strip()
        org = (row.get(company_col, "") if company_col else "").strip()
        return (who or "(no name)"), org

    # ---- box 1: open action items - column F date, column G task.
    # Anything dated before today is left out; newest date first.
    actions = []
    for row in rows:
        raw = (row.get(date_col, "") if date_col else "").strip()
        if not raw:
            continue

        when, _ = parse_when(raw)
        if not when:
            continue                       # no readable date, so nothing to schedule by

        day = when.replace(hour=0, minute=0, second=0, microsecond=0)
        if day < today:
            continue                       # already in the past

        who, org = label(row)
        actions.append({
            "who": who, "org": org,
            "raw": raw,
            "when": day,
            "shown": pretty_when(day, False),
            "task": (row.get(task_col, "") if task_col else "").strip(),
            "colour": priority_of(row.get(prio_col, "") if prio_col else ""),
            "status": "today" if day == today else "upcoming",
            "days": (day - today).days,
            "row": row.get("_row", 0),
        })

    actions.sort(key=lambda a: a["when"], reverse=True)

    # ---- box 1 fallback: calls and follow-ups from columns
    followups = []
    for row in rows:
        task = (row.get(followup_col, "") if followup_col else "").strip()
        due_raw = (row.get(due_col, "") if due_col else "").strip()
        due = parse_any_date(due_raw)
        if not task and not due:
            continue
        who, org = label(row)
        overdue = bool(due and due < today)
        days = (today - due).days if overdue else 0
        followups.append({
            "who": who, "org": org,
            "task": task or "Follow up",
            "due_raw": due_raw,
            "due": due,
            "overdue": overdue,
            "days_over": days,
        })

    followups.sort(key=lambda f: (f["due"] is None, f["due"] or today))

    # ---- box 2: gone quiet for more than CRM_STALE_DAYS
    # Dates come from the Activity notes first, falling back to a date column.
    stale = []
    for row in rows:
        activity = (row.get(activity_col, "") if activity_col else "").strip()
        last, last_line = crm.last_activity_date(activity)
        source = "activity"

        if not last:
            last_raw = (row.get(last_col, "") if last_col else "").strip()
            last = parse_any_date(last_raw)
            last_line = last_raw
            source = "column"

        if last and last >= cutoff:
            continue                       # contacted recently enough

        who, org = label(row)
        if who == "(no name)" and not org:
            continue

        stale.append({
            "who": who, "org": org,
            "last_raw": last.strftime("%b %d, %Y") if last else "",
            "last_line": last_line,
            "last": last,
            "source": source,
            "days": (today - last).days if last else None,
        })

    stale.sort(key=lambda s: (s["last"] is None, s["last"] or today))

    return {
        "actions": actions,
        "followups": followups,
        "stale": stale,
        "stale_days": CRM_STALE_DAYS,
        "cols": {
            "name": name_col, "company": company_col,
            "followup": followup_col, "due": due_col, "last": last_col,
            "activity": activity_col, "date": date_col, "task": task_col,
        },
    }


# ---------------------------------------------------------------- workers

def log(message):
    print(message)
    state["log"].append(message)


def news_log(message):
    print(message)
    news_state["log"].append(message)


def crm_log(message):
    print(message)
    crm_state["log"].append(message)


def worker(target, country_code):
    country = contact_agent.country_name(country_code) if country_code else ""
    try:
        findings = contact_agent.run_agent(target, country_code=country_code, log=log)
        log("Sending Telegram notification...")
        notify.run_finished(target, country, findings, log=log)
    except Exception as e:
        log(f"ERROR: {e}")
        notify.run_finished(target, country, [], error=str(e), log=log)
    finally:
        state["running"] = False


def news_worker(push=True):
    try:
        items = news.run_news(log=news_log)
        news_state["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        if push:
            news_log("Sending Telegram digest...")
            notify.news_digest(items, log=news_log)
    except Exception as e:
        news_log(f"ERROR: {e}")
    finally:
        news_state["running"] = False


def li_log(message):
    print(message)
    li_state["log"].append(message)


def li_comment_worker(force=False):
    try:
        li_log("Drafting comments...")
        linkedin.draft_li_all(log=li_log, force=force)
        li_state["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    except Exception as e:
        li_log(f"ERROR: {e}")
    finally:
        li_state["running"] = False


def li_worker(force=False):
    try:
        linkedin.run_daily(log=li_log, force=force)
        li_state["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    except Exception as e:
        li_log(f"ERROR: {e}")
    finally:
        li_state["running"] = False


def start_li_run(force=False):
    if li_state["running"]:
        return False
    li_state["running"] = True
    li_state["log"] = []
    threading.Thread(target=li_worker, args=(force,), daemon=True).start()
    return True


def x_log(message):
    print(message)
    x_state["log"].append(message)


def x_worker(force=False):
    try:
        x_log("Drafting replies...")
        if force:
            xcom.draft_all(log=x_log, force=True)
        else:
            xcom.refresh(log=x_log)
        x_state["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    except Exception as e:
        x_log(f"ERROR: {e}")
    finally:
        x_state["running"] = False


def start_x_run(force=False):
    if x_state["running"]:
        return False
    x_state["running"] = True
    x_state["log"] = []
    threading.Thread(target=x_worker, args=(force,), daemon=True).start()
    return True


def yt_log(message):
    print(message)
    yt_state["log"].append(message)


def yt_worker():
    try:
        youtube.refresh(log=yt_log)
        yt_state["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    except Exception as e:
        yt_log(f"ERROR: {e}")
    finally:
        yt_state["running"] = False


def start_yt_run():
    if yt_state["running"]:
        return False
    yt_state["running"] = True
    yt_state["log"] = []
    threading.Thread(target=yt_worker, daemon=True).start()
    return True


def crm_worker(sheet):
    try:
        headers, rows = store.read_sheet_auto(sheet)
        cols = crm_boxes(headers, rows)["cols"]
        crm.analyze(sheet, rows, cols, log=crm_log)
        crm_state["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    except Exception as e:
        crm_log(f"ERROR: {e}")
    finally:
        crm_state["running"] = False


def news_cooldown_left():
    if not news_state["last_run_ts"]:
        return 0
    elapsed = time.time() - news_state["last_run_ts"]
    return max(0, int(NEWS_COOLDOWN - elapsed))


def start_news_run(push=True, force=False):
    if news_state["running"]:
        return False
    if not force and news_cooldown_left() > 0:
        return False
    news_state["running"] = True
    news_state["last_run_ts"] = time.time()
    news_state["log"] = []
    threading.Thread(target=news_worker, args=(push,), daemon=True).start()
    return True


def scheduler():
    """Daily jobs: the news digest and the LinkedIn post."""
    news_done = None
    li_done = None
    yt_done = None
    while True:
        clock = datetime.now().strftime("%H:%M")
        today = date.today()

        if clock == NEWS_TIME and news_done != today:
            news_done = today
            print(f"[scheduler] daily news run at {NEWS_TIME}")
            start_news_run(push=True, force=True)

        if clock == LINKEDIN_TIME and li_done != today:
            li_done = today
            print(f"[scheduler] daily LinkedIn post at {LINKEDIN_TIME}")
            start_li_run(force=True)

        if clock == YOUTUBE_TIME and yt_done != today:
            yt_done = today
            print(f"[scheduler] daily YouTube refresh at {YOUTUBE_TIME}")
            start_yt_run()

        time.sleep(30)


threading.Thread(target=scheduler, daemon=True).start()


# ---------------------------------------------------------------- routes

@app.route("/")
def home():
    all_rows = contact_agent.read_rows()
    company = request.args.get("company", "")
    q = request.args.get("q", "")
    visible = apply_filters(all_rows, company, q)

    return render_template_string(
        CONTACTS_PAGE,
        rows=visible,
        total=len(all_rows),
        companies=company_list(all_rows),
        countries=contact_agent.COUNTRIES,
        company=company,
        q=q,
        no_company=NO_COMPANY,
        contacted_count=sum(1 for r in visible if r["contacted"] == "yes"),
        fields=contact_agent.DISPLAY_FIELDS,
        editable=contact_agent.EDITABLE_FIELDS,
        backup=store.backup_info(),
        workbook=store.XLSX_FILE,
        has_logo=logo_exists(),
        notify_status=notify.status_line(),
        tab="contacts",
        state=state,
    )


@app.route("/news")
def news_page():
    items = news.latest()
    return render_template_string(
        NEWS_PAGE,
        groups=news.group_by_category(items),
        total_items=len(items),
        saved_items=news.saved(),
        saved_ids=news.saved_ids(),
        has_logo=logo_exists(),
        notify_status=notify.status_line(),
        news_time=NEWS_TIME,
        cooldown_left=news_cooldown_left(),
        tab="news",
        state=news_state,
    )


@app.route("/crm")
def crm_page():
    sheets = store.custom_sheets()
    sheet = request.args.get("sheet", "") or (CRM_SHEET if CRM_SHEET in sheets
                                              else (sheets[0] if sheets else CRM_SHEET))
    q = request.args.get("q", "").strip()
    adding = request.args.get("add", "") == "1"

    try:                                   # how many cells the last Save wrote, if any
        saved = int(request.args["saved"])
    except (KeyError, ValueError):
        saved = None

    headers, all_rows = store.read_sheet_auto(sheet)

    # Priority lives in column H unless that column is already in use
    prio_col, prio_note = priority_column(headers) if headers else (0, "")
    if prio_col and not find_priority_col(headers):
        failed = write_cells(sheet, [(1, prio_col, CRM_PRIORITY_HEADER)])
        if failed:
            prio_note = (f"Could not write the {CRM_PRIORITY_HEADER} header into "
                         f"{col_letter(prio_col)}1 of {sheet} - see the terminal.")
        else:
            print(f"added a {CRM_PRIORITY_HEADER} column at "
                  f"{col_letter(prio_col)}1 on sheet {sheet}")
            headers, all_rows = store.read_sheet_auto(sheet)

    prio_key = headers[prio_col - 1] if 0 < prio_col <= len(headers) else ""

    total = len(all_rows)

    boxes = crm_boxes(headers, all_rows)
    suggestions = crm.merge(all_rows, boxes["cols"], sheet)

    # each entry is (excel column number, real header key, display label)
    all_cols = [(i + 1, h, CRM_COL_LABELS.get(i + 1, h))
                for i, h in enumerate(headers)
                if (i + 1) not in CRM_HIDE_COLS]

    columns = all_cols[:CRM_MAX_COLS]
    wide = (boxes["cols"]["activity"] or CRM_WIDE_COL).strip().lower()

    # excel column numbers of the date (F) and action item (G) cells, so both can be edited
    date_key = boxes["cols"]["date"]
    task_key = boxes["cols"]["task"]
    date_col = (headers.index(date_key) + 1) if date_key in headers else 0
    task_col = (headers.index(task_key) + 1) if task_key in headers else 0

    shown_nums = [c[0] for c in columns]
    for num in (date_col, task_col, prio_col) + tuple(CRM_EXTRA_COLS):
        if num and num not in shown_nums:
            for col in all_cols:
                if col[0] == num:
                    columns.append(col)
                    shown_nums.append(num)
                    break

    # the sheet may not carry a Priority header yet - show the column anyway
    if prio_col and prio_col not in shown_nums:
        columns.append((prio_col, prio_key or CRM_PRIORITY_HEADER, CRM_PRIORITY_HEADER))
        shown_nums.append(prio_col)

    for col in all_cols[CRM_MAX_COLS:]:
        if wide and wide == col[1].strip().lower() and col[0] not in shown_nums:
            columns.append(col)
            shown_nums.append(col[0])

    columns.sort(key=lambda c: c[0])          # keep spreadsheet order

    # Priority lives at the end of the sheet but is shown right after S.No.
    if prio_col:
        prio_tuple = next((c for c in columns if c[0] == prio_col), None)
        if prio_tuple:
            columns.remove(prio_tuple)
            after_sno = 1 if columns and columns[0][0] == CRM_SNO_COL else 0
            columns.insert(after_sno, prio_tuple)

    # phone / email / location go last, after the big text boxes
    extras = [c for c in columns if c[0] in CRM_EXTRA_COLS]
    for col in extras:
        columns.remove(col)
    columns.extend(extras)

    name_key = boxes["cols"]["name"]
    name_col = (headers.index(name_key) + 1) if name_key in headers else 0

    # next serial number for the add row
    sno_key = headers[CRM_SNO_COL - 1] if len(headers) >= CRM_SNO_COL else ""
    next_sno = ""
    if sno_key:
        nums = []
        for r in all_rows:
            raw = (r.get(sno_key, "") or "").strip()
            try:
                nums.append(int(float(raw)))
            except ValueError:
                continue
        next_sno = str(max(nums) + 1) if nums else "1"

    rows = all_rows
    if q:
        needle = q.lower()
        rows = [r for r in rows
                if any(needle in (v or "").lower()
                       for k, v in r.items() if k != "_row")]

    # --- sorting
    sort = request.args.get("sort", "")
    direction = request.args.get("dir", "asc")

    sortable = [
        ("S.No.", sno_key or "_row"),
        ("Company", boxes["cols"]["company"]),
        ("Name", boxes["cols"]["name"]),
        ("Action", task_key),
        ("Date", date_key),
        ("Priority", prio_key),
    ]
    sortable = [(label, col) for label, col in sortable if col]

    if sort == "_row":
        rows = sorted(rows, key=lambda r: r.get("_row", 0),
                      reverse=(direction == "desc"))
    elif sort and sort == date_key:
        # dates sort chronologically, blanks last whichever way round
        far = datetime.max if direction == "asc" else datetime.min

        def date_key_of(row):
            when, _ = parse_when(row.get(sort, ""))
            return when or far

        rows = sorted(rows, key=date_key_of, reverse=(direction == "desc"))
    elif sort and sort == prio_key:
        # red first, then yellow, green, blue
        rows = sorted(rows,
                      key=lambda r: PRIORITY_ORDER[priority_of(r.get(sort, ""))],
                      reverse=(direction == "desc"))
    elif sort and sort in headers:
        rows = sorted(rows, key=lambda r: _sort_key(r.get(sort, "")),
                      reverse=(direction == "desc"))

    return render_template_string(
        CRM_PAGE,
        sheets=sheets,
        sheet=sheet,
        columns=columns,
        hidden_count=max(0, len(all_cols) - len(columns)),
        rows=rows,
        total=total,
        q=q,
        adding=adding,
        boxes=boxes,
        suggestions=suggestions,
        wide_col=wide,
        date_col=date_col,
        task_col=task_col,
        prio_col=prio_col,
        prio_note=prio_note,
        name_col=name_col,
        edit_cols=list(CRM_EXTRA_COLS),
        saved=saved,
        workbook=store.XLSX_FILE,
        backup=store.backup_info(),
        has_logo=logo_exists(),
        notify_status=notify.status_line(),
        tab="crm",
        state=crm_state,
        sortable=sortable,
        sort=sort,
        direction=direction,
        next_sno=next_sno,
        sno_col=CRM_SNO_COL,
    )


@app.route("/crm/analyze", methods=["POST"])
def crm_analyze():
    sheet = request.form.get("sheet", "")
    if sheet and not crm_state["running"]:
        crm_state["running"] = True
        crm_state["sheet"] = sheet
        crm_state["log"] = []
        threading.Thread(target=crm_worker, args=(sheet,), daemon=True).start()
    return redirect(url_for("crm_page", sheet=sheet))


@app.route("/crm/journey")
def crm_journey():
    """One person's activity notes, parsed into a journey the page can draw."""
    sheet = request.args.get("sheet", "")
    try:
        excel_row = int(request.args.get("row", "0"))
    except ValueError:
        excel_row = 0
    if not sheet or not excel_row:
        return jsonify({"error": "need a sheet and a row"}), 400

    headers, rows = store.read_sheet_auto(sheet)
    row = next((r for r in rows if r.get("_row", 0) == excel_row), None)
    if row is None:
        return jsonify({"error": "that row is not on the sheet any more"}), 404

    boxes = crm_boxes(headers, rows)
    activity_key = boxes["cols"]["activity"] or CRM_WIDE_COL
    name_key = boxes["cols"]["name"]
    company_key = boxes["cols"]["company"]
    prio_num = find_priority_col(headers)

    points = parse_activity(row.get(activity_key, ""))
    return jsonify({
        "name": str(row.get(name_key, "") or "").strip() or "(no name)",
        "company": str(row.get(company_key, "") or "").strip(),
        "colour": priority_of(row.get(headers[prio_num - 1], "") if prio_num else ""),
        "points": points,
        "months": journey_months(points),
        "summary": journey_summary(points),
        "levels": list(JOURNEY_LEVELS),
    })


@app.route("/crm/save", methods=["POST"])
def crm_save():
    """Write every edited cell on the sheet in one go. Unchanged cells are left alone."""
    sheet = request.form.get("sheet", "")
    saved = 0

    if sheet:
        headers, rows = store.read_sheet_auto(sheet)

        # which column is the calendar one - its stored text and the picker's text differ in shape
        date_key = headers[CRM_DATE_COL_NUM - 1] if len(headers) >= CRM_DATE_COL_NUM else ""
        if CRM_DATE_COL:
            date_key = find_col(headers, (), CRM_DATE_COL) or date_key
        date_num = (headers.index(date_key) + 1) if date_key in headers else 0
        prio_num, _ = priority_column(headers)
        if prio_num and not find_priority_col(headers):
            write_cells(sheet, [(1, prio_num, CRM_PRIORITY_HEADER)])
            headers, rows = store.read_sheet_auto(sheet)

        current = {}
        for row in rows:
            for i, header in enumerate(headers):
                current[(row.get("_row", 0), i + 1)] = (row.get(header, "") or "")

        wrote = []                                  # (row, col, value) for the check below

        for field, posted in request.form.items():
            if not field.startswith("cell_"):
                continue
            try:
                _, row_text, col_text = field.split("_", 2)
                excel_row, col = int(row_text), int(col_text)
            except ValueError:
                continue

            new = (posted or "").strip()
            old = (current.get((excel_row, col), "") or "").strip()
            if col == date_num:
                old = iso_date(old) or old          # compare like with like
            elif col == prio_num:
                new = priority_label(priority_of(new))
                old = priority_label(priority_of(old))
            if new == old:
                continue

            wrote.append((excel_row, col, new))
            saved += 1

        # write them, check each one stuck, and force through anything that did not
        if wrote:
            write_cells(sheet, wrote)

    return redirect(url_for("crm_page",
                            sheet=sheet,
                            q=request.form.get("q", "") or None,
                            sort=request.form.get("sort", "") or None,
                            dir=request.form.get("dir", "") or None,
                            saved=saved))


@app.route("/crm/edit", methods=["POST"])
def crm_edit():
    sheet = request.form.get("sheet", "")
    try:
        excel_row = int(request.form.get("row", "0"))
        col = int(request.form.get("col", "0"))
    except ValueError:
        excel_row = col = 0
    if sheet and excel_row and col:
        store.update_cell(sheet, excel_row, col, request.form.get("value", "").strip())
    return redirect(url_for("crm_page", sheet=sheet, q=request.form.get("q", "") or None))


@app.route("/x")
def x_page():
    return render_template_string(
        X_PAGE,
        posts=xcom.recent(),
        history=xcom.history(15),
        total=len(xcom.all_posts()),
        handle=xcom.HANDLE,
        max_age=xcom.MAX_AGE_DAYS,
        top_n=xcom.TOP_N,
        api_ready=xcom.api_ready(),
        x_status=xcom.status_line(),
        setup=xcom.setup_notes(),
        has_logo=logo_exists(),
        notify_status=notify.status_line(),
        tab="x",
        state=x_state,
    )


@app.route("/x/run", methods=["POST"])
def x_run():
    start_x_run(force=request.form.get("force") == "1")
    return redirect(url_for("x_page"))


@app.route("/x/add", methods=["POST"])
def x_add():
    """Save the pasted posts, then draft their replies straight away."""
    blob = request.form.get("bulk", "").strip()
    added = 0
    x_state["log"] = []

    if blob:
        added = xcom.add_bulk(blob, log=x_log)
    else:
        url = request.form.get("url", "").strip()
        text = request.form.get("text", "").strip()
        if url or text:
            added = 1 if xcom.add_post(url, text, log=x_log) else 0

    if added and not x_state["running"]:
        x_state["running"] = True
        threading.Thread(target=x_worker, daemon=True).start()
    elif not added:
        x_log("Nothing was added - check the link is an x.com/<handle>/status/<id> URL "
              "and the post text is on the lines below it.")

    return redirect(url_for("x_page"))


@app.route("/x/redraft/<post_id>", methods=["POST"])
def x_redraft(post_id):
    xcom.redraft_one(post_id, log=x_log)
    return redirect(url_for("x_page"))


@app.route("/x/commented/<post_id>", methods=["POST"])
def x_commented(post_id):
    xcom.toggle_commented(post_id)
    return redirect(url_for("x_page"))


@app.route("/x/delete/<post_id>", methods=["POST"])
def x_delete(post_id):
    xcom.delete_post(post_id)
    return redirect(url_for("x_page"))


@app.route("/youtube")
def youtube_page():
    rows = youtube.decorate(youtube.videos())
    sort = request.args.get("sort", "published")
    direction = request.args.get("dir", "desc")

    keys = {
        "published": lambda r: r.get("published_raw", ""),
        "views": lambda r: r.get("views_n", 0),
        "likes": lambda r: r.get("likes_n", 0),
        "comments": lambda r: r.get("comments_n", 0),
        "engagement": lambda r: r.get("engagement", 0),
        "title": lambda r: (r.get("title") or "").lower(),
    }
    if sort in keys:
        rows = sorted(rows, key=keys[sort], reverse=(direction == "desc"))

    return render_template_string(
        YOUTUBE_PAGE,
        rows=rows,
        chan=youtube.channel(),
        totals=youtube.totals(rows),
        sort=sort,
        direction=direction,
        channel_url=youtube.CHANNEL_URL,
        yt_status=youtube.status_line(),
        refresh_time=YOUTUBE_TIME,
        has_logo=logo_exists(),
        notify_status=notify.status_line(),
        tab="youtube",
        state=yt_state,
    )


@app.route("/youtube/run", methods=["POST"])
def youtube_run():
    start_yt_run()
    return redirect(url_for("youtube_page"))


@app.route("/linkedin")
def linkedin_page():
    post = linkedin.latest()
    return render_template_string(
        LINKEDIN_PAGE,
        post=post,
        past=[p for p in linkedin.history()[1:13]],
        engage=linkedin.comments_recent(),
        engage_total=len(linkedin.comments_all()),
        engage_days=linkedin.COMMENT_MAX_AGE_DAYS,
        today=datetime.now().strftime("%Y-%m-%d"),
        post_time=LINKEDIN_TIME,
        has_logo=logo_exists(),
        notify_status=notify.status_line(),
        tab="linkedin",
        state=li_state,
    )


@app.route("/linkedin/run", methods=["POST"])
def linkedin_run():
    start_li_run(force=True)
    return redirect(url_for("linkedin_page"))


@app.route("/linkedin/add", methods=["POST"])
def linkedin_add():
    """Save pasted posts, then draft their comments straight away."""
    li_state["log"] = []
    blob = request.form.get("bulk", "").strip()
    added = linkedin.comment_add_bulk(blob, log=li_log) if blob else 0

    if added and not li_state["running"]:
        li_state["running"] = True
        threading.Thread(target=li_comment_worker, daemon=True).start()
    elif not added:
        li_log("Nothing was added - paste a linkedin.com post link with the post "
               "text on the lines below it.")
    return redirect(url_for("linkedin_page"))


@app.route("/linkedin/draft", methods=["POST"])
def linkedin_draft():
    if not li_state["running"]:
        li_state["running"] = True
        li_state["log"] = []
        force = request.form.get("force") == "1"
        threading.Thread(target=li_comment_worker, args=(force,), daemon=True).start()
    return redirect(url_for("linkedin_page"))


@app.route("/linkedin/redraft/<post_id>", methods=["POST"])
def linkedin_redraft(post_id):
    linkedin.redraft_li_one(post_id, log=li_log)
    return redirect(url_for("linkedin_page"))


@app.route("/linkedin/commented/<post_id>", methods=["POST"])
def linkedin_commented(post_id):
    linkedin.comment_toggle(post_id)
    return redirect(url_for("linkedin_page"))


@app.route("/linkedin/remove/<post_id>", methods=["POST"])
def linkedin_remove(post_id):
    linkedin.comment_delete(post_id)
    return redirect(url_for("linkedin_page"))


@app.route("/linkedin/push", methods=["POST"])
def linkedin_push():
    post = linkedin.latest()
    if post:
        notify.send(
            f"\U0001F4DD <b>Today's LinkedIn post</b>\n\n"
            f"{post.get('post', '')}\n\n"
            f"Source: {post.get('title', '')}\n{post.get('url', '')}",
            log=li_log,
        )
    return redirect(url_for("linkedin_page"))


@app.route("/contacts/backup", methods=["POST"])
def contacts_backup():
    store.backup_now()
    return redirect(url_for("home",
                            company=request.form.get("company", "") or None,
                            q=request.form.get("q", "") or None))


@app.route("/contacts/restore", methods=["POST"])
def contacts_restore():
    store.restore_backup()
    return redirect(url_for("home",
                            company=request.form.get("company", "") or None,
                            q=request.form.get("q", "") or None))


@app.route("/crm/backup", methods=["POST"])
def crm_backup():
    store.backup_now()
    return redirect(url_for("crm_page", sheet=request.form.get("sheet", "")))


@app.route("/crm/restore", methods=["POST"])
def crm_restore():
    store.restore_backup()
    return redirect(url_for("crm_page", sheet=request.form.get("sheet", "")))

@app.route("/crm/done/<int:excel_row>", methods=["POST"])
def crm_done(excel_row):
    sheet = request.form.get("sheet", "")
    if sheet:
        crm.dismiss(sheet, excel_row)
    return redirect(url_for("crm_page", sheet=sheet))

@app.route("/crm/add", methods=["POST"])
def crm_add():
    sheet = request.form.get("sheet", "")
    values = {}
    for key, val in request.form.items():
        if key.startswith("col_"):
            try:
                values[int(key[4:])] = val.strip()
            except ValueError:
                continue
    if sheet and any(values.values()):
        store.append_blank_row(sheet, values)
    return redirect(url_for("crm_page", sheet=sheet))


@app.route("/news/run", methods=["POST"])
def news_run():
    start_news_run(push=True)
    return redirect(url_for("news_page"))


@app.route("/news/save/<news_id>", methods=["POST"])
def news_save(news_id):
    news.save_item(news_id)
    return redirect(url_for("news_page"))


@app.route("/news/unsave/<news_id>", methods=["POST"])
def news_unsave(news_id):
    news.unsave_item(news_id)
    return redirect(url_for("news_page"))


@app.route("/news/push/<news_id>", methods=["POST"])
def news_push(news_id):
    item = news.find_item(news_id)
    if item:
        notify.news_item(item, log=news_log)
    return redirect(url_for("news_page"))


@app.route("/news/push-saved", methods=["POST"])
def news_push_saved():
    items = news.saved()
    if items:
        notify.news_digest(items, log=news_log)
    return redirect(url_for("news_page"))


@app.route("/news/test-notify", methods=["POST"])
def news_test_notify():
    notify.send("Test from Hire2o AI News", log=news_log)
    return redirect(url_for("news_page"))


@app.route("/run", methods=["POST"])
def run():
    target = request.form.get("target", "").strip()
    country_code = request.form.get("country_code", "")
    if country_code not in contact_agent.COUNTRIES:
        country_code = ""
    if target and not state["running"]:
        state["running"] = True
        state["target"] = target
        state["country"] = country_code
        state["log"] = []
        threading.Thread(target=worker, args=(target, country_code), daemon=True).start()
    return back(request.form)


@app.route("/edit/<row_id>", methods=["POST"])
def edit(row_id):
    contact_agent.update_field(row_id,
                               request.form.get("field", ""),
                               request.form.get("value", ""))
    return back(request.form)


@app.route("/test-notify", methods=["POST"])
def test_notify():
    notify.send("Test message from Hire2o Contact Finder", log=log)
    return back(request.form)


@app.route("/delete/<row_id>", methods=["POST"])
def delete(row_id):
    contact_agent.delete_row(row_id)
    return back(request.form)


@app.route("/toggle/<row_id>", methods=["POST"])
def toggle(row_id):
    contact_agent.toggle_contacted(row_id)
    return back(request.form)


@app.route("/clear", methods=["POST"])
def clear():
    if not state["running"]:
        contact_agent.clear_all()
    return redirect(url_for("home"))


# ---------------------------------------------------------------- templates

STYLE = """
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>

.donebtn:hover { background: var(--green-soft); color:#15803d; border-color:#b7e0c4; }
  
    /* ============================================================
       BRAND PALETTE - change these five lines to re-theme the app
       ============================================================ */
    :root {
      --brand:        #0f2f5c;
      --brand-light:  #1c4a8a;
      --accent:       #00a8a8;
      --accent-dark:  #00807f;
      --accent-soft:  #e4f6f6;

      --ink:          #17212e;
      --ink-soft:     #5b6878;
      --ink-faint:    #8a95a3;
      --line:         #e3e8ee;
      --line-soft:    #eef1f5;
      --surface:      #ffffff;
      --canvas:       #f5f7fa;

      --green:        #16a34a;
      --green-soft:   #eaf7ee;
      --amber:        #b7791f;
      --amber-soft:   #fdf6e3;
      --red:          #dc2626;
      --red-soft:     #fdeced;

      --radius:       10px;
      --shadow:       0 1px 2px rgba(16,32,56,.06), 0 2px 8px rgba(16,32,56,.05);
      --shadow-lift:  0 2px 6px rgba(16,32,56,.09), 0 8px 24px rgba(16,32,56,.07);
    }

    * { box-sizing: border-box; }

    body {
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
      background: var(--canvas); color: var(--ink); margin: 0;
      font-size: 14px; line-height: 1.5; -webkit-font-smoothing: antialiased;
    }

    .shell { max-width: 1640px; margin: 0 auto; padding: 0 28px 60px; }

    /* ---------------------------------------------------- header */
    .masthead {
      background: var(--brand);
      background-image: linear-gradient(135deg, var(--brand) 0%, #143a70 100%);
      color: #fff; position: sticky; top: 0; z-index: 50;
      box-shadow: 0 1px 0 rgba(255,255,255,.08), 0 2px 14px rgba(9,24,48,.22);
    }
    .masthead-inner {
      max-width: 1640px; margin: 0 auto; padding: 0 28px;
      display: flex; align-items: center; gap: 26px; height: 62px;
    }
    .brandmark { display:flex; align-items:center; gap:11px; text-decoration:none; }
    .brandmark img { height: 32px; width:auto; display:block; }
    .brandmark .name { font-size: 19px; font-weight: 700; color:#fff; letter-spacing:-0.3px; }
    .brandmark .sub {
      font-size: 12px; font-weight: 500; color: rgba(255,255,255,.62);
      border-left: 1px solid rgba(255,255,255,.22); padding-left: 11px; margin-left: 2px;
    }

    .nav { display:flex; gap:4px; margin-left: 10px; }
    .nav a {
      color: rgba(255,255,255,.76); text-decoration:none; font-size:14px; font-weight:500;
      padding: 7px 15px; border-radius: 7px; transition: all .13s;
    }
    .nav a:hover { color:#fff; background: rgba(255,255,255,.1); }
    .nav a.on { color: var(--brand); background: #fff; font-weight:600; }

    .masthead .spacer { flex: 1; }
    .statuspill {
      font-size: 12px; color: rgba(255,255,255,.8); display:flex; align-items:center; gap:7px;
      background: rgba(255,255,255,.1); padding: 5px 12px; border-radius: 99px;
    }
    .dot { width:7px; height:7px; border-radius:50%; background: var(--ink-faint); }
    .dot.live { background:#3ddc97; box-shadow:0 0 0 3px rgba(61,220,151,.22); }

    /* ---------------------------------------------------- panels */
    .panel {
      background: var(--surface); border:1px solid var(--line);
      border-radius: var(--radius); box-shadow: var(--shadow);
    }
    .panel-pad { padding: 20px 22px; }
    .panel + .panel { margin-top: 18px; }

    .searchbar { margin-top: 24px; }
    .searchrow { display:flex; gap:11px; align-items:center; flex-wrap:wrap; }
    .fieldlabel {
      display:block; font-size:11px; font-weight:600; letter-spacing:.05em;
      text-transform:uppercase; color: var(--ink-faint); margin-bottom:6px;
    }

    input[type=text], select, textarea {
      font-family: inherit; font-size: 14px; color: var(--ink);
      border: 1px solid var(--line); border-radius: 8px;
      padding: 10px 12px; background: #fff; transition: border-color .13s, box-shadow .13s;
    }
    input[type=text]:focus, select:focus, textarea:focus {
      outline:none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft);
    }
    input[type=text]:disabled, select:disabled { background:#f6f8fa; color: var(--ink-faint); }
    #target { flex: 1 1 400px; min-width: 280px; }
    #q { width: 280px; }
    select { min-width: 210px; cursor:pointer; }

    /* ---------------------------------------------------- buttons */
    .btn {
      font-family: inherit; font-size: 14px; font-weight: 500; line-height:1;
      border: 1px solid var(--line); background: #fff; color: var(--ink);
      border-radius: 8px; padding: 11px 16px; cursor: pointer;
      transition: all .13s; white-space: nowrap; text-decoration: none; display: inline-block;
    }
    .btn:hover { border-color: #c9d2dd; background: #fafbfc; }
    .btn:active { transform: translateY(1px); }
    .btn:disabled { opacity:.5; cursor: not-allowed; transform:none; }

    .btn-primary {
      background: var(--accent); border-color: var(--accent); color:#fff; font-weight:600;
      box-shadow: 0 1px 3px rgba(0,168,168,.3);
    }
    .btn-primary:hover:not(:disabled) { background: var(--accent-dark); border-color: var(--accent-dark); }
    .btn-sm { font-size: 12.5px; padding: 7px 11px; border-radius:7px; }
    .btn-icon { padding: 7px 10px; font-size: 14px; }
    .btn-ghost { border-color: transparent; background: transparent; color: var(--ink-soft); }
    .btn-ghost:hover { background: var(--line-soft); border-color: transparent; }
    .btn-danger:hover { background: var(--red-soft); border-color:#f0b4b4; color: var(--red); }
    .btn-on { background: var(--green); border-color: var(--green); color:#fff; }
    .btn-on:hover { background:#128a3e; border-color:#128a3e; }
    .btn-star { color: var(--amber); border-color:#e8d9b0; background: var(--amber-soft); }

    form.inline { display:inline-block; margin:0; }

    /* ---------------------------------------------------- banner + log */
    .banner {
      display:flex; align-items:center; gap:11px;
      background: var(--amber-soft); border:1px solid #ecd9a4;
      border-left: 4px solid var(--amber);
      color:#7c5b13; padding: 13px 17px; border-radius: 8px; margin: 16px 0; font-size: 13.5px;
    }
    .pulse {
      width:9px; height:9px; border-radius:50%; background: var(--amber);
      animation: pulse 1.3s ease-in-out infinite; flex-shrink:0;
    }
    @keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:.3; } }

    .log {
      background: #0e1a2b; color: #93e6c8;
      font-family: ui-monospace, 'SF Mono', Menlo, monospace; font-size: 12.5px;
      line-height:1.65; padding: 15px 17px; border-radius: var(--radius);
      height: 168px; overflow:auto; white-space: pre-wrap; margin: 16px 0;
      box-shadow: inset 0 1px 6px rgba(0,0,0,.34);
    }
    .log::-webkit-scrollbar { width: 9px; }
    .log::-webkit-scrollbar-thumb { background: #2b4055; border-radius: 5px; }

    /* ---------------------------------------------------- section head */
    .sectionhead { display:flex; align-items:baseline; gap:13px; margin: 26px 0 13px; }
    .sectionhead h2 {
      font-size: 17px; font-weight: 650; color: var(--brand); margin:0; letter-spacing:-0.2px;
    }
    .counts { font-size: 13px; color: var(--ink-faint); }
    .chip {
      display:inline-block; background: var(--accent-soft); color: var(--accent-dark);
      font-size: 12px; font-weight: 600; padding: 3px 9px; border-radius: 99px;
    }

    /* ---------------------------------------------------- table */
    .tablewrap {
      background: var(--surface); border:1px solid var(--line);
      border-radius: var(--radius); box-shadow: var(--shadow);
      max-height: calc(100vh - 210px); overflow: auto;
    }
    table { border-collapse: separate; border-spacing:0; width:100%; font-size: 13.5px; }
    thead th {
      background: #f7f9fb; color: var(--ink-soft);
      font-size: 11px; font-weight: 600; letter-spacing:.055em; text-transform: uppercase;
      text-align:left; padding: 11px 12px; white-space: nowrap;
      border-bottom: 1px solid var(--line); position: sticky; top: 0; z-index: 5;
    }
    tbody td {
      padding: 9px 12px; vertical-align: top;
      border-bottom: 1px solid var(--line-soft); height: 1px;
    }
    tbody tr:hover { background: #fbfcfd; }
    tbody tr:last-child td { border-bottom: none; }

    tr.done { background: var(--green-soft); }
    tr.done:hover { background: #e2f3e8; }
    tr.done td:first-child { box-shadow: inset 3px 0 0 var(--green); }
    tr.done td { color: #4c6555; }

    td a { color: var(--accent-dark); text-decoration: none; }
    td a:hover { text-decoration: underline; }
    .url { word-break: break-all; font-size: 11.5px; display:inline-block; max-width: 270px; }
    .srcname { font-weight: 600; color: var(--brand-light); }
    .muted { color: var(--ink-faint); font-weight: 400; }
    .datecell { white-space: nowrap; font-variant-numeric: tabular-nums; color: var(--ink-soft); }

    /* the wide column (Activity by default) - a fixed 45-character box */
    th.wide4 { min-width: calc(45ch + 42px); }
    td.wide4 {
      width: 1%; min-width: calc(45ch + 42px);
      white-space: nowrap; line-height: 1.55; vertical-align: top;
    }
    td.wide4 textarea.cellin {
      width: 45ch; min-width: 45ch; max-width: 45ch;
      overflow: hidden; min-height: 0;
      white-space: pre-wrap; word-break: break-word;
    }

    .badge {
      display:inline-block; font-size: 11px; font-weight: 600;
      padding: 2px 9px; border-radius: 99px; text-transform: capitalize;
    }
    .badge-high   { background: var(--green-soft); color: #15803d; }
    .badge-medium { background: var(--amber-soft); color: var(--amber); }
    .badge-low    { background: var(--line-soft);  color: var(--ink-faint); }

    /* ---------------------------------------------------- editable cells */
    .cellform { margin:0; }
    .cellin {
      border: 1px solid transparent; background: transparent; font: inherit;
      color: inherit; padding: 5px 7px; border-radius: 6px; width: 100%; transition: all .12s;
    }
    .cellin:hover { border-color: var(--line); background:#fff; }
    .cellin:focus {
      border-color: var(--accent); background:#fff; outline:none; box-shadow: 0 0 0 3px var(--accent-soft);
    }
    .cellin::placeholder { color: #c3ccd6; }
    textarea.cellin { resize: vertical; font-size: 12.5px; line-height:1.45; display: block; }
    td.editable { min-width: 155px; }
    td.commentcell { min-width: 225px; max-width: 270px; }

    /* ---------------------------------------------------- news */
    .newswrap { display: grid; grid-template-columns: minmax(0,1fr) 390px; gap: 24px; align-items:start; }
    @media (max-width: 1150px) { .newswrap { grid-template-columns: 1fr; } }

    .card {
      background: var(--surface); border:1px solid var(--line); border-radius: var(--radius);
      padding: 20px 22px; margin-bottom: 16px; box-shadow: var(--shadow);
      transition: box-shadow .16s, transform .16s;
    }
    .card:hover { box-shadow: var(--shadow-lift); transform: translateY(-1px); }
    .card h3 { margin: 0 0 9px; font-size: 17px; line-height:1.38; font-weight: 650; letter-spacing:-0.2px; }
    .card h3 a { color: var(--brand); text-decoration:none; }
    .card h3 a:hover { color: var(--accent-dark); }
    .card p { margin: 0 0 13px; font-size: 14.5px; line-height: 1.62; color: #3a4658; }

    .meta { display:flex; align-items:center; gap:9px; font-size: 12px; color: var(--ink-faint); margin-bottom: 11px; flex-wrap:wrap; }
    .sourcetag {
      background: var(--accent-soft); color: var(--accent-dark);
      font-weight: 600; padding: 3px 9px; border-radius: 5px; font-size: 11.5px;
    }
    .cardlink { font-size: 12px; word-break: break-all; color: var(--ink-faint); text-decoration:none; }
    .cardlink:hover { color: var(--accent-dark); }
    .cardactions { display:flex; gap:8px; margin-top: 15px; padding-top: 14px; border-top: 1px solid var(--line-soft); }

    .savedcol {
      background: var(--surface); border:1px solid var(--line); border-radius: var(--radius);
      box-shadow: var(--shadow); position: sticky; top: 82px; overflow: hidden;
    }
    .savedhead { background: linear-gradient(135deg, var(--brand) 0%, #143a70 100%); color:#fff; padding: 15px 18px; }
    .savedhead h2 { margin:0 0 3px; font-size: 15px; font-weight: 650; }
    .savedhead .n { font-size: 12px; color: rgba(255,255,255,.66); }
    .savedbody { padding: 12px 16px 16px; }
    .savedscroll { max-height: 62vh; overflow-y: auto; margin: 0 -6px; padding: 0 6px; }
    .savedscroll::-webkit-scrollbar { width: 7px; }
    .savedscroll::-webkit-scrollbar-thumb { background: #d2dae3; border-radius: 4px; }
    .savedcard { padding: 13px 0; border-bottom: 1px solid var(--line-soft); }
    .savedcard:last-child { border-bottom:none; }
    .savedcard > a {
      font-size: 13.5px; font-weight: 600; color: var(--brand); text-decoration:none;
      line-height:1.42; display:block; margin-bottom: 5px;
    }
    .savedcard > a:hover { color: var(--accent-dark); }

    .empty { text-align:center; padding: 46px 22px; color: var(--ink-faint); }
    .empty .big { font-size: 34px; margin-bottom: 10px; opacity:.4; }
    .empty p { margin: 0; font-size: 14px; }

    /* ---------------------------------------------------- news sections */
    .jumpbar { display:flex; flex-wrap:wrap; gap:7px; margin: 20px 0 4px; }
    .jumpbar a {
      font-size: 12.5px; font-weight: 500; text-decoration:none;
      color: var(--ink-soft); background: var(--surface);
      border:1px solid var(--line); border-radius: 99px; padding: 6px 13px; transition: all .13s;
    }
    .jumpbar a:hover { border-color: var(--accent); color: var(--accent-dark); background: var(--accent-soft); }
    .jumpbar a .n { color: var(--ink-faint); font-weight: 600; margin-left: 4px; }

    .catblock { margin-top: 30px; scroll-margin-top: 78px; }
    .cathead {
      display:flex; align-items:center; gap:11px;
      padding: 0 0 11px; margin-bottom: 15px; border-bottom: 2px solid var(--line);
    }
    .cathead .bar { width: 4px; height: 20px; border-radius: 3px; background: var(--accent); }
    .cathead h2 { margin:0; font-size: 16px; font-weight: 700; color: var(--brand); letter-spacing: -0.2px; }
    .cathead .n {
      font-size: 11.5px; font-weight: 600; color: var(--accent-dark);
      background: var(--accent-soft); padding: 2px 9px; border-radius: 99px;
    }
    .cathead .top { margin-left:auto; font-size:12px; color: var(--ink-faint); text-decoration:none; }
    .cathead .top:hover { color: var(--accent-dark); }

    .cat-0 .bar { background:#6366f1; }
    .cat-1 .bar { background:#0ea5e9; }
    .cat-2 .bar { background:#8b5cf6; }
    .cat-3 .bar { background:#0f766e; }
    .cat-4 .bar { background:#16a34a; }
    .cat-5 .bar { background:#ea580c; }
    .cat-6 .bar { background:#0891b2; }
    .cat-7 .bar { background:#b45309; }
    .cat-8 .bar { background:#dc2626; }
    .cat-9 .bar { background:#db2777; }

    .cattag {
      display:inline-block; font-size: 11px; font-weight: 600; color: var(--ink-faint);
      background: var(--line-soft); padding: 2px 8px; border-radius: 4px;
    }

    /* ---------------------------------------------------- CRM action boxes */
    .boxrow { display:grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-top: 24px; }
    @media (max-width: 1050px) { .boxrow { grid-template-columns: 1fr; } }

    /* CRM page: table on the left, action boxes in a thin sticky right column */
    .crmwrap {
      display:grid; grid-template-columns: minmax(0, 1fr) 320px;
      gap: 20px; align-items: start; margin-top: 20px;
    }
    .crmmain { grid-column: 1; grid-row: 1; min-width: 0; }
    .crmside {
      grid-column: 2; grid-row: 1;
      display:flex; flex-direction:column; gap: 16px;
      position: sticky; top: 14px;
    }
    .crmside .boxhead { padding: 11px 13px; gap: 7px; }
    .crmside .boxhead h2 { font-size: 13px; line-height: 1.3; }
    .crmside .boxhead .icon { font-size: 15px; }
    .crmside .boxhead .n { font-size: 11px; padding: 2px 8px; }
    .crmside .boxscroll {
      max-height: calc(50vh - 130px); min-height: 90px; padding: 2px 13px 10px;
    }
    .crmside .boxempty { padding: 18px 13px; font-size: 12.5px; }
    .crmside .boxnote { padding: 9px 13px; line-height: 1.5; }
    .crmside .boxnote form { flex-wrap: wrap; gap: 7px; }
    .crmside .sugg .action { font-size: 12.5px; }
    .crmside .boxitem .what { font-size: 12.5px; }
    .crmside .boxitem .who, .crmside .sugg .who { font-size: 13px; }
    @media (max-width: 1250px) {
      .crmwrap { grid-template-columns: 1fr; }
      .crmmain, .crmside { grid-column: auto; grid-row: auto; }
      .crmside { position: static; }
      .crmside .boxscroll { max-height: 300px; }
    }

    .actionbox {
      background: var(--surface); border:1px solid var(--line); border-radius: var(--radius);
      box-shadow: var(--shadow); overflow: hidden; display:flex; flex-direction:column;
    }
    .boxhead {
      padding: 14px 18px; display:flex; align-items:center; gap:10px;
      border-bottom: 1px solid var(--line);
    }
    .boxhead .icon { font-size: 17px; }
    .boxhead h2 { margin:0; font-size: 15px; font-weight: 650; color: var(--brand); }
    .boxhead .n {
      margin-left:auto; font-size: 11.5px; font-weight: 700;
      padding: 3px 10px; border-radius: 99px;
    }
    .box-calls .boxhead { background: linear-gradient(180deg, #fffaf0 0%, #fff 100%); }
    .box-calls .boxhead .n { background: var(--amber-soft); color: var(--amber); }
    .box-stale .boxhead { background: linear-gradient(180deg, #f2f8ff 0%, #fff 100%); }
    .box-stale .boxhead .n { background: var(--accent-soft); color: var(--accent-dark); }

    .boxscroll { max-height: 320px; overflow-y: auto; padding: 4px 18px 14px; }
    .boxscroll::-webkit-scrollbar { width: 7px; }
    .boxscroll::-webkit-scrollbar-thumb { background: #d2dae3; border-radius: 4px; }

    .boxitem { padding: 11px 0; border-bottom: 1px solid var(--line-soft); }
    .boxitem:last-child { border-bottom:none; }
    .boxitem .who, .sugg .who { font-weight: 600; font-size: 13.5px; color: var(--brand); }
    .boxitem .org, .sugg .org { font-size: 12.5px; color: var(--ink-faint); margin-left: 4px; }
    .boxitem .what { font-size: 13px; color: #3a4658; margin-top: 3px; line-height:1.45; }
    .boxitem .when { font-size: 11.5px; color: var(--ink-faint); margin-top: 4px; line-height:1.5; }

    .pill {
      display:inline-block; font-size: 10.5px; font-weight: 700; letter-spacing:.03em;
      padding: 2px 8px; border-radius: 99px; text-transform: uppercase; margin-left: 7px;
    }
    .pill-over { background: var(--red-soft); color: var(--red); }
    .pill-soon { background: var(--amber-soft); color: var(--amber); }
    .pill-cold { background: #eef1f5; color: var(--ink-soft); }
    .pill-high { background: var(--red-soft); color: var(--red); }
    .pill-medium { background: var(--amber-soft); color: var(--amber); }
    .pill-low { background: var(--line-soft); color: var(--ink-faint); }
    .pill-stale { background:#f3e8ff; color:#7e22ce; }

    /* scheduled calls read from the action date/time column */
    .act .when { font-variant-numeric: tabular-nums; }
    .act-overdue .when { color: var(--red); font-weight: 600; }
    .act-today .when { color: var(--amber); font-weight: 600; }
    .act-undated .when { color: var(--ink-faint); font-style: italic; }
    .boxsub {
      font-size: 10.5px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase;
      color: var(--ink-faint); padding: 11px 0 2px; margin-top: 5px;
      border-top: 1px solid var(--line-soft);
    }
    input.whenin {
      font-variant-numeric: tabular-nums; font-size: 13px;
      padding: 7px 9px; min-width: 128px; cursor: pointer;
    }
    input[type=date].whenin::-webkit-calendar-picker-indicator { cursor: pointer; opacity: .55; }
    input[type=date].whenin:hover::-webkit-calendar-picker-indicator { opacity: 1; }
    /* expenses */
    .exprow { display: flex; flex-wrap: wrap; gap: 14px; align-items: flex-end; }
    .expfield { flex: 0 1 165px; min-width: 130px; }
    .expfield.wide { flex: 1 1 220px; }
    .expfield.narrow { flex: 0 1 120px; min-width: 100px; }
    .expfield input[type=text], .expfield input[type=date], .expfield select { width: 100%; }
    .expfield input[type=file] { font-size: 12.5px; width: 100%; }
    .checkline {
      display: inline-flex; align-items: center; gap: 7px;
      font-size: 13px; color: var(--ink-soft); white-space: nowrap; padding-bottom: 9px;
    }
    .chip-soft { background: var(--canvas); color: var(--ink-soft); font-weight: 500; }
    td.money, th.money { text-align: right; font-variant-numeric: tabular-nums; }
    input.amountin { width: 88px; text-align: right; font-variant-numeric: tabular-nums; }
    td.editcell select.cellin { width: 118px; }
    tr.grouptotal td {
      background: var(--canvas); font-weight: 600; color: var(--brand); font-size: 12.5px;
    }
    td.receiptcell { text-align: center; width: 1%; white-space: nowrap; }
    .receiptthumb {
      width: 40px; height: 40px; object-fit: cover; border-radius: 6px;
      border: 1px solid var(--line); display: block;
    }
    .receipticon { font-size: 22px; }
    .receiptform { margin: 0; }
    .receiptadd {
      display: inline-flex; align-items: center; justify-content: center;
      width: 34px; height: 34px; border: 1px dashed var(--line); border-radius: 8px;
      cursor: pointer; font-size: 17px; color: var(--ink-faint);
    }
    .receiptadd:hover { border-color: var(--accent); color: var(--accent); background: var(--accent-soft); }
    .receiptadd input[type=file] { display: none; }

    /* priority swatch */
    td.priocell, th.priocell { width: 1%; white-space: nowrap; text-align: center; }
    .prio {
      width: 18px; height: 18px; padding: 0; border-radius: 5px;
      border: 1px solid rgba(15, 47, 92, .22); cursor: pointer;
      display: inline-block; vertical-align: middle; transition: transform .1s, box-shadow .1s;
    }
    .prio:hover { transform: scale(1.18); box-shadow: 0 1px 5px rgba(15,47,92,.25); }
    .legend {
      display: inline-flex; align-items: center; gap: 12px; flex-wrap: nowrap;
      font-size: 12px; color: var(--ink-soft); white-space: nowrap;
    }
    .legend span { display: inline-flex; align-items: center; white-space: nowrap; }
    .dot {
      width: 11px; height: 11px; border-radius: 3px; flex: none;
      display: inline-block; vertical-align: middle;
      border: 1px solid rgba(15, 47, 92, .22);
    }
    .legend .dot { margin-right: 5px; }
    .prio-blue   { background: #2563eb; }
    .prio-red    { background: #dc2626; }
    .prio-yellow { background: #eab308; }
    .prio-green  { background: #16a34a; }

    /* action item: a fixed 30-character box, 3 lines tall */
    td.taskcell {
      width: 1%; min-width: calc(30ch + 42px);
      white-space: nowrap; vertical-align: top;
    }
    td.taskcell textarea.taskin {
      width: 30ch; min-width: 30ch; max-width: 30ch;
      resize: vertical; overflow: auto; white-space: pre-wrap; word-break: break-word;
      font-size: 13px; line-height: 1.45; display: block;
    }
    /* keep ordinary CRM cells from hogging the width and pushing the table sideways */
    .crmmain tbody td { max-width: 140px; word-break: break-word; }
    .crmmain tbody td.taskcell, .crmmain tbody td.wide4,
    .crmmain tbody td.editcell { max-width: none; }
    .crmmain tbody td.namecell { max-width: 15ch; }
    td.namecell, th.namecell {
      width: 15ch; min-width: 15ch; max-width: 15ch;
      white-space: normal; word-break: break-word; vertical-align: top;
    }
    /* clickable name -> relationship journey */
    .namelink {
      background: none; border: 0; padding: 0; margin: 0; text-align: left;
      font: inherit; color: var(--brand); font-weight: 600; cursor: pointer;
      text-decoration: underline; text-decoration-color: rgba(15, 47, 92, .28);
      text-underline-offset: 2px; width: 100%;
    }
    .namelink:hover { color: var(--accent); text-decoration-color: var(--accent); }

    .jmodal {
      display: none; position: fixed; inset: 0; z-index: 90;
      background: rgba(15, 47, 92, .45); padding: 28px 18px; overflow: auto;
    }
    .jmodal.open { display: block; }
    .jcard {
      background: #fff; border-radius: 14px; max-width: 1040px; margin: 0 auto;
      box-shadow: 0 18px 50px rgba(15, 47, 92, .3); overflow: hidden;
    }
    .jhead {
      display: flex; align-items: flex-start; gap: 14px;
      padding: 18px 22px; border-bottom: 1px solid var(--line-soft);
    }
    .jhead h2 { margin: 0; font-size: 18px; color: var(--brand); }
    .jsub { font-size: 13px; color: var(--ink-faint); margin-top: 2px; }
    .jclose {
      margin-left: auto; background: none; border: 0; font-size: 26px; line-height: 1;
      color: var(--ink-faint); cursor: pointer; padding: 0 4px;
    }
    .jclose:hover { color: var(--brand); }
    .jbody { padding: 18px 22px 24px; position: relative; }

    .jstats { display: flex; flex-wrap: wrap; gap: 26px; margin-bottom: 16px; }
    .jstat span { display: block; font-size: 12px; color: var(--ink-faint); }
    .jstat b { font-size: 26px; color: var(--brand); font-weight: 700; }

    .jlegend {
      display: flex; flex-wrap: wrap; align-items: center; gap: 14px;
      font-size: 12px; color: var(--ink-soft); margin-bottom: 10px;
    }
    .jlegend span { display: inline-flex; align-items: center; }
    .jlegend i { width: 11px; height: 11px; border-radius: 3px; margin-right: 6px; }
    .jshapes { color: var(--ink-faint); }

    .jsvg { width: 100%; height: auto; display: block; }
    .jaxis { font-size: 12px; fill: #7a8699; font-family: inherit; }
    .jh3 { font-size: 13px; color: var(--ink-soft); margin: 14px 0 2px; font-weight: 600; }
    .jempty { color: var(--ink-faint); font-size: 13.5px; padding: 26px 2px; }
    .jempty span { font-size: 12.5px; }

    .jtip {
      display: none; position: absolute; z-index: 5; max-width: 320px;
      background: var(--brand); color: #fff; font-size: 12.5px; line-height: 1.45;
      padding: 8px 11px; border-radius: 8px; pointer-events: none;
      box-shadow: 0 6px 18px rgba(15, 47, 92, .3);
    }
    .jpill {
      display: inline-block; margin-left: 7px; padding: 1px 7px; border-radius: 99px;
      font-size: 10.5px; font-weight: 700; text-transform: uppercase; letter-spacing: .04em;
    }

    .clamp3 {
      display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical;
      overflow: hidden; line-height: 1.4; max-height: 4.2em;
    }
    td.editcell { min-width: 175px; width: 1%; white-space: nowrap; }
    td.editcell input.cellin { width: 175px; font-size: 13px; }
    .crmmain thead th { white-space: normal; }
    td.taskcell textarea.cellin { overflow: hidden; min-height: 0; line-height: 1.45; }

    /* save bar */
    .savebar {
      display:flex; align-items:center; gap: 12px; flex-wrap: wrap;
      margin: 0 0 12px;
    }
    .savebar-bottom { margin: 14px 0 0; }
    .warnline {
      font-size: 12.5px; color: var(--amber); background: var(--amber-soft);
      border-radius: 8px; padding: 8px 12px; margin-bottom: 12px;
    }
    .savestate { font-size: 12.5px; color: var(--ink-faint); }
    .savestate.dirty { color: var(--amber); font-weight: 600; }
    .savedflash {
      font-size: 12.5px; font-weight: 600; color: #15803d;
      background: var(--green-soft); padding: 4px 11px; border-radius: 99px;
    }
    .cellin.touched { background: #fffdf3; border-color: var(--amber); }
    .rawnote {
      font-size: 11px; color: var(--red); margin-top: 3px; padding-left: 2px;
      word-break: break-word;
    }

    .boxempty { padding: 26px 18px; text-align:center; color: var(--ink-faint); font-size: 13px; }
    .boxnote { font-size: 11.5px; color: var(--ink-faint); padding: 10px 18px; border-top: 1px solid var(--line-soft); }

    .sugg { padding: 12px 0; border-bottom: 1px solid var(--line-soft); }
    .sugg:last-child { border-bottom:none; }
    .sugg .action {
      font-size: 13.5px; color: var(--ink); margin-top: 4px; line-height:1.5;
      padding-left: 10px; border-left: 3px solid var(--line);
    }
    .sugg.p-high .action { border-left-color: var(--red); }
    .sugg.p-medium .action { border-left-color: var(--amber); }
    .sugg.p-low .action { border-left-color: var(--line); }
    .timing { font-size: 11.5px; color: var(--ink-faint); margin-top: 4px; padding-left: 13px; }


    /* ---------------------------------------------------- LinkedIn page */
    .liwrap { display:grid; grid-template-columns: minmax(0,1.05fr) minmax(0,1fr); gap: 20px; margin-top: 22px; }
    @media (max-width: 1100px) { .liwrap { grid-template-columns: 1fr; } }

    .libox {
      background: var(--surface); border:1px solid var(--line); border-radius: var(--radius);
      box-shadow: var(--shadow); overflow:hidden; display:flex; flex-direction:column;
    }
    .libox .boxhead { background: linear-gradient(180deg, #f6fbfb 0%, #fff 100%); }
    .libox .boxhead .n { background: var(--accent-soft); color: var(--accent-dark); }
    .libody { padding: 18px 20px 20px; }

    .arttitle { font-size: 17px; font-weight: 650; color: var(--brand); line-height:1.4; margin: 0 0 9px; }
    .arttitle a { color: var(--brand); text-decoration:none; }
    .arttitle a:hover { color: var(--accent-dark); text-decoration: underline; }
    .artsum { font-size: 13.5px; color: #3a4658; line-height:1.6; margin: 0 0 14px; }

    .posttext {
      font-size: 15px; line-height: 1.68; color: var(--ink);
      white-space: pre-wrap; background: #fbfcfd; border:1px solid var(--line);
      border-radius: 8px; padding: 16px 18px; max-height: 340px; overflow-y:auto;
    }
    .posttext::-webkit-scrollbar { width: 7px; }
    .posttext::-webkit-scrollbar-thumb { background:#d2dae3; border-radius:4px; }

    .wordcount { font-size: 11.5px; font-weight: 600; padding: 2px 9px; border-radius: 99px; }
    .wc-ok { background: var(--green-soft); color:#15803d; }
    .wc-off { background: var(--amber-soft); color: var(--amber); }

    .shareimg {
      width: 100%; height: auto; display:block; border-radius: 8px;
      border:1px solid var(--line); box-shadow: var(--shadow);
    }
    .imgnote { font-size: 11.5px; color: var(--ink-faint); margin-top: 10px; line-height:1.5; }

    .pastrow {
      display:flex; gap:12px; align-items:baseline; padding: 10px 0;
      border-bottom: 1px solid var(--line-soft); font-size: 13px;
    }
    .pastrow:last-child { border-bottom:none; }
    .pastrow .d { color: var(--ink-faint); font-size:11.5px; white-space:nowrap;
                  font-variant-numeric: tabular-nums; min-width: 78px; }
    .pastrow a { color: var(--brand); text-decoration:none; font-weight:500; }
    .pastrow a:hover { color: var(--accent-dark); text-decoration: underline; }

    .copybtn.done { background: var(--green); border-color: var(--green); color:#fff; }


    /* ---------------------------------------------------- YouTube page */
    .tilerow { display:grid; grid-template-columns: repeat(auto-fit, minmax(160px,1fr));
               gap:14px; margin-top:22px; }
    .tile { background:var(--surface); border:1px solid var(--line); border-radius:var(--radius);
            box-shadow:var(--shadow); padding:16px 18px; }
    .tile .k { font-size:11px; font-weight:600; letter-spacing:.055em; text-transform:uppercase;
               color:var(--ink-faint); margin-bottom:6px; }
    .tile .v { font-size:25px; font-weight:700; color:var(--brand); letter-spacing:-0.5px;
               line-height:1.1; font-variant-numeric:tabular-nums; }
    .tile .s { font-size:11.5px; color:var(--ink-faint); margin-top:4px; }
    .tile.accent .v { color:var(--accent-dark); }

    .chanbar { display:flex; align-items:center; gap:16px; margin-top:22px;
               background:var(--surface); border:1px solid var(--line);
               border-radius:var(--radius); box-shadow:var(--shadow); padding:16px 20px; }
    .chanbar img { width:58px; height:58px; border-radius:50%; object-fit:cover; flex-shrink:0; }
    .chanbar .nm { font-size:17px; font-weight:650; color:var(--brand); }
    .chanbar .hd { font-size:12.5px; color:var(--ink-faint); margin-top:2px; }

    .thumb { width:132px; height:74px; object-fit:cover; border-radius:6px;
             border:1px solid var(--line); display:block; }
    .vidtitle { font-size:14px; font-weight:600; color:var(--brand); line-height:1.4;
                text-decoration:none; display:block; }
    .vidtitle:hover { color:var(--accent-dark); text-decoration:underline; }
    .vidmeta { font-size:11.5px; color:var(--ink-faint); margin-top:5px; }
    .num { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
    .num b { font-size:14px; font-weight:650; }
    .engage { font-weight:650; font-variant-numeric:tabular-nums; }
    .eng-hi { color:#15803d; }
    .eng-mid { color:var(--amber); }
    .eng-lo { color:var(--ink-faint); }
    th.sortable a { color:inherit; text-decoration:none; }
    th.sortable a:hover { color:var(--accent-dark); }
    th.sortable.on a { color:var(--accent-dark); }
    .setupbox { background:var(--amber-soft); border:1px solid #ecd9a4; border-left:4px solid var(--amber);
                border-radius:8px; padding:18px 22px; margin-top:22px; color:#6b4f12; }
    .setupbox h3 { margin:0 0 10px; font-size:15px; color:#7c5b13; }
    .setupbox ol { margin:0; padding-left:20px; font-size:13.5px; line-height:1.9; }
    .setupbox code { background:rgba(0,0,0,.07); padding:2px 6px; border-radius:4px;
                     font-family:ui-monospace,Menlo,monospace; font-size:12.5px; }


    /* ---------------------------------------------------- X page */
    .xpost { background:var(--surface); border:1px solid var(--line);
             border-radius:var(--radius); box-shadow:var(--shadow);
             padding:18px 20px; margin-bottom:15px; }
    .xhead { display:flex; align-items:center; gap:9px; margin-bottom:10px; flex-wrap:wrap; }
    .xauthor { font-weight:650; font-size:14px; color:var(--brand); text-decoration:none; }
    .xauthor:hover { color:var(--accent-dark); text-decoration:underline; }
    .xage { font-size:12px; color:var(--ink-faint); }
    .xmetrics { margin-left:auto; display:flex; gap:12px; font-size:12px;
                color:var(--ink-faint); font-variant-numeric:tabular-nums; }
    .xtext { font-size:14.5px; line-height:1.6; color:#2c3645; white-space:pre-wrap;
             word-break:break-word; }
    .xreply { margin-top:14px; padding:13px 15px; background:var(--accent-soft);
              border-left:3px solid var(--accent); border-radius:0 8px 8px 0; }
    .xreply .lbl { font-size:10.5px; font-weight:700; letter-spacing:.06em;
                   text-transform:uppercase; color:var(--accent-dark); margin-bottom:5px; }
    .xreply .body { font-size:14px; line-height:1.55; color:var(--ink); white-space:pre-wrap; }
    .xreply .wc { font-size:11px; color:var(--ink-faint); margin-top:6px; }
    .xnone { margin-top:12px; font-size:13px; color:var(--ink-faint); font-style:italic; }
    .xactions { display:flex; gap:7px; margin-top:13px; padding-top:12px;
                border-top:1px solid var(--line-soft); flex-wrap:wrap;
                align-items:center; }
    .pastebox { width:100%; min-height:92px; font-family:inherit; font-size:13.5px;
                line-height:1.5; padding:11px 13px; border:1px solid var(--line);
                border-radius:8px; resize:vertical; }
    .pastehint { font-size:12px; color:var(--ink-faint); margin:8px 0 0; line-height:1.6; }
    .xpost.done { background:var(--green-soft); border-color:#bfe3c9; }
    .xpost.done .xtext, .xpost.done .xreply .body { color:#5b7566; }
    .xpost.done .xreply { background:#e4f3e8; border-left-color:var(--green); }
    .xlink { display:block; font-size:12px; color:var(--accent-dark); word-break:break-all;
             margin-top:10px; text-decoration:none; }
    .xlink:hover { text-decoration:underline; }
    .donecheck { display:flex; align-items:center; gap:7px; cursor:pointer;
                 font-size:13px; font-weight:600; color:var(--ink-soft);
                 border:1px solid var(--line); border-radius:8px; padding:7px 12px;
                 background:#fff; transition:all .13s; }
    .donecheck:hover { border-color:var(--green); color:#15803d; }
    .donecheck input { width:16px; height:16px; margin:0; cursor:pointer; accent-color:var(--green); }
    .donecheck.on { background:var(--green); border-color:var(--green); color:#fff; }
    .xauthor.nohandle { color:var(--amber); font-weight:500; font-size:12.5px; }
    .xreply .toh { color:var(--brand); font-weight:700; }
    .xwait { margin-top:14px; padding:12px 15px; background:var(--amber-soft);
             border-left:3px solid var(--amber); border-radius:0 8px 8px 0;
             font-size:13.5px; color:#7c5b13; display:flex; align-items:center; gap:9px; }
                 .histrow { padding:14px 20px; border-bottom:1px solid var(--line-soft); }
    .histrow:last-child { border-bottom:none; }
    .histmeta { display:flex; align-items:center; gap:10px; margin-bottom:7px; flex-wrap:wrap; }
    .histmeta .xage { font-variant-numeric:tabular-nums; }
    .histlink { font-size:12px; color:var(--accent-dark); text-decoration:none; }
    .histlink:hover { text-decoration:underline; }
    .histreply { font-size:13.5px; line-height:1.55; color:var(--ink);
                 padding-left:11px; border-left:3px solid var(--green); }
    .histpost { font-size:12px; color:var(--ink-faint); margin-top:7px; line-height:1.5; }
    .btn-trash { color:var(--ink-faint); }
    .btn-trash:hover { background:var(--red-soft); border-color:#f0b4b4; color:var(--red); }

    /* ---------------------------------------------------- CRM add row */
    .addrow { background: var(--accent-soft); }
    .addrow td { padding: 10px 12px; }
    .addrow input, .addrow textarea {
      width: 100%; font-size: 13px; padding: 7px 9px;
      border: 1px solid #b9dede; border-radius: 6px; background:#fff;
    }
    .hiddennote {
      font-size: 11.5px; color: var(--ink-faint); margin-left: 6px;
      background: var(--line-soft); padding: 2px 8px; border-radius: 4px;
    }

        .sortbar { margin-left:auto; display:flex; align-items:center; gap:6px; }
    .sortlabel { font-size:11px; font-weight:600; letter-spacing:.05em;
                 text-transform:uppercase; color:var(--ink-faint); margin-right:2px; }
    .sortbtn { font-size:12.5px; font-weight:500; text-decoration:none; color:var(--ink-soft);
               background:var(--surface); border:1px solid var(--line); border-radius:99px;
               padding:5px 13px; transition:all .13s; white-space:nowrap; }
    .sortbtn:hover { border-color:var(--accent); color:var(--accent-dark); background:var(--accent-soft); }
    .sortbtn.on { background:var(--accent); border-color:var(--accent); color:#fff; font-weight:600; }
    .sortbtn.clear { color:var(--ink-faint); border-style:dashed; }

  </style>
"""

HEADER = """
  <div class="masthead">
    <div class="masthead-inner">
      <a class="brandmark" href="/">
        {% if has_logo %}
          <img src="{{ url_for('static', filename='logo.png') }}" alt="Hire2o">
        {% else %}
          <span class="name">Hire2o</span>
        {% endif %}
      </a>

      <nav class="nav">
        <a href="/" class="{% if tab == 'contacts' %}on{% endif %}">Contacts</a>
        <a href="/news" class="{% if tab == 'news' %}on{% endif %}">AI News</a>
        <a href="/crm" class="{% if tab == 'crm' %}on{% endif %}">CRM</a>
        <a href="/linkedin" class="{% if tab == 'linkedin' %}on{% endif %}">LinkedIn Post</a>
        <a href="/youtube" class="{% if tab == 'youtube' %}on{% endif %}">YouTube</a>
        <a href="/x" class="{% if tab == 'x' %}on{% endif %}">X</a>
        <a href="/events" class="{% if tab == 'events' %}on{% endif %}">Events</a>
        <a href="/expenses" class="{% if tab == 'expenses' %}on{% endif %}">Expenses</a>
      </nav>

      <div class="spacer"></div>

      <div class="statuspill">
        <span class="dot {% if notify_status != 'off' %}live{% endif %}"></span>
        {{ notify_status }}
      </div>
    </div>
  </div>
"""

CONTACTS_PAGE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Hire2o - Contacts</title>
  {% if state.running %}<meta http-equiv="refresh" content="30">{% endif %}
""" + STYLE + """
</head>
<body>
""" + HEADER + """
<div class="shell">

  <div class="panel panel-pad searchbar">
    <form method="post" action="/run">
      <input type="hidden" name="company" value="{{ company }}">
      <input type="hidden" name="q" value="{{ q }}">
      <div class="searchrow">
        <div style="flex:1 1 400px; min-width:280px;">
          <label class="fieldlabel" for="target">Who are you looking for</label>
          <input type="text" id="target" name="target" style="width:100%;"
                 placeholder="e.g. VP of Engineering at Acme Corp"
                 {% if state.running %}disabled{% endif %} autofocus>
        </div>
        <div>
          <label class="fieldlabel" for="country_code">Country</label>
          <select id="country_code" name="country_code" {% if state.running %}disabled{% endif %}>
            {% for code, info in countries.items() %}
              <option value="{{ code }}" {% if code == state.country %}selected{% endif %}>{{ info[0] }}</option>
            {% endfor %}
          </select>
        </div>
        <div style="align-self:flex-end; display:flex; gap:8px;">
          <button class="btn btn-primary" type="submit" {% if state.running %}disabled{% endif %}>
            {% if state.running %}Searching...{% else %}Find contacts{% endif %}
          </button>
          {% if notify_status != 'off' %}
            <button class="btn btn-ghost" type="submit" formaction="/test-notify">Test alert</button>
          {% endif %}
        </div>
      </div>
    </form>
  </div>

  {% if state.running %}
    <div class="banner">
      <span class="pulse"></span>
      <span>Searching for <b>{{ state.target }}</b>{% if state.country %} in <b>{{ countries[state.country][0] }}</b>{% endif %} - this page refreshes every 3 seconds.</span>
    </div>
  {% endif %}

  {% if state.log %}
    <div class="log">{% for line in state.log %}{{ line }}
{% endfor %}</div>
  {% endif %}

  <div class="panel panel-pad" style="margin-top:18px;">
    <form method="get" action="/">
      <div class="searchrow">
        <div>
          <label class="fieldlabel" for="company">Filter by company</label>
          <select id="company" name="company" onchange="this.form.submit()">
            <option value="">All companies ({{ total }})</option>
            {% for c in companies %}
              <option value="{{ c }}" {% if c == company %}selected{% endif %}>{{ c }}</option>
            {% endfor %}
            <option value="{{ no_company }}" {% if company == no_company %}selected{% endif %}>(no company listed)</option>
          </select>
        </div>
        <div>
          <label class="fieldlabel" for="q">Search all columns</label>
          <input type="text" id="q" name="q" value="{{ q }}" placeholder="name, note, email, date...">
        </div>
        <div style="align-self:flex-end; display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
          <button class="btn" type="submit">Search</button>
          {% if company or q %}<a href="/" style="font-size:13px; color:var(--ink-soft);">Clear filters</a>{% endif %}
          <button class="btn" type="submit" formaction="/contacts/backup" formmethod="post"
                  title="Copy {{ workbook }} to results_backup.xlsx now">&#128190; Back up now</button>
          {% if backup.exists %}
            <button class="btn btn-danger" type="submit" formaction="/contacts/restore" formmethod="post"
                    onclick="return confirm('Replace {{ workbook }} with the backup from {{ backup.when }}?\\n\\nAnything saved since then will be lost.');">
              &#8634; Restore
            </button>
          {% endif %}
        </div>
      </div>
      <p class="muted" style="font-size:12.5px; margin:12px 0 0;">
        {% if backup.exists %}
          Backup last written <b>{{ backup.when }}</b>
          ({{ (backup.size / 1024) | round(1) }} KB) &middot; one is also taken automatically before every save.
        {% else %}
          No backup yet - press <b>Back up now</b> to make one.
        {% endif %}
      </p>
    </form>
  </div>

  <div class="sectionhead">
    <h2>Contacts</h2>
    <span class="counts">showing {{ rows|length }} of {{ total }} &middot; newest first</span>
    {% if contacted_count %}<span class="chip">{{ contacted_count }} contacted</span>{% endif %}
  </div>

  {% if rows %}
    <div class="tablewrap">
      <table>
        <thead>
          <tr>
            <th>Done</th><th></th>
            {% for f in fields %}<th>{{ f.replace('_', ' ') }}</th>{% endfor %}
          </tr>
        </thead>
        <tbody>
        {% for row in rows %}
        <tr class="{% if row.contacted == 'yes' %}done{% endif %}">
          <td>
            <form class="inline" method="post" action="/toggle/{{ row.row_id }}">
              <input type="hidden" name="company" value="{{ company }}">
              <input type="hidden" name="q" value="{{ q }}">
              <button class="btn btn-icon {% if row.contacted == 'yes' %}btn-on{% endif %}" type="submit"
                      title="{% if row.contacted == 'yes' %}Mark as not contacted{% else %}Mark as contacted{% endif %}">&#10003;</button>
            </form>
          </td>
          <td>
            <form class="inline" method="post" action="/delete/{{ row.row_id }}"
                  onsubmit="return confirm('Delete {{ row.name|e }}?');">
              <input type="hidden" name="company" value="{{ company }}">
              <input type="hidden" name="q" value="{{ q }}">
              <button class="btn btn-icon btn-danger" type="submit" title="Delete row">&#128465;</button>
            </form>
          </td>

          {% for f in fields %}
            {% set value = row.get(f, '') %}
            {% if f == 'comment' %}
              <td class="commentcell">
                <form class="cellform" method="post" action="/edit/{{ row.row_id }}">
                  <input type="hidden" name="field" value="comment">
                  <input type="hidden" name="company" value="{{ company }}">
                  <input type="hidden" name="q" value="{{ q }}">
                  <textarea class="cellin" name="value" rows="2" placeholder="Add a note..."
                            onblur="if(this.defaultValue!==this.value){this.form.submit();}">{{ value }}</textarea>
                </form>
              </td>
            {% elif f in editable %}
              <td class="editable">
                <form class="cellform" method="post" action="/edit/{{ row.row_id }}">
                  <input type="hidden" name="field" value="{{ f }}">
                  <input type="hidden" name="company" value="{{ company }}">
                  <input type="hidden" name="q" value="{{ q }}">
                  <input class="cellin" type="text" name="value" value="{{ value }}" placeholder="-"
                         onblur="if(this.defaultValue!==this.value){this.form.submit();}">
                </form>
                {% if f == 'email' and value %}
                  <a href="mailto:{{ value }}" style="font-size:11.5px; padding-left:7px;">Compose &#8599;</a>
                {% endif %}
              </td>
            {% elif f == 'confidence' %}
              <td>{% if value %}<span class="badge badge-{{ value|lower }}">{{ value }}</span>{% endif %}</td>
            {% elif f in ('search_date', 'found_at') %}
              <td class="datecell">{{ value }}</td>
            {% else %}
              <td>
                {% if f == 'linkedin' and value %}
                  <a class="url" href="{{ value }}" target="_blank" rel="noopener noreferrer">{{ value }}</a>
                {% elif f == 'source_url' and value %}
                  <a href="{{ value }}" target="_blank" rel="noopener noreferrer" title="{{ value }}">
                    <span class="srcname">{{ value | domain }}</span> &#8599;</a>
                  <div class="url muted">{{ value }}</div>
                {% else %}
                  {{ value }}
                {% endif %}
              </td>
            {% endif %}
          {% endfor %}
        </tr>
        {% endfor %}
        </tbody>
      </table>
    </div>

    <form method="post" action="/clear" style="margin-top:18px;">
      <button class="btn btn-danger" type="submit" {% if state.running %}disabled{% endif %}>Clear all contacts</button>
    </form>

  {% elif total %}
    <div class="panel empty">
      <div class="big">&#128269;</div>
      <p>No contacts match this filter. <a href="/" style="color:var(--accent-dark);">Clear filters</a> to see all {{ total }}.</p>
    </div>
  {% else %}
    <div class="panel empty">
      <div class="big">&#128100;</div>
      <p>No contacts yet. Describe who you're looking for above and press <b>Find contacts</b>.</p>
    </div>
  {% endif %}

</div>
</body>
</html>
"""

NEWS_PAGE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Hire2o - AI News</title>
  {% if state.running %}<meta http-equiv="refresh" content="4">
  {% elif cooldown_left %}<meta http-equiv="refresh" content="20">{% endif %}
""" + STYLE + """
</head>
<body>
""" + HEADER + """
<div class="shell">

  <div class="panel panel-pad searchbar">
    <form method="post" action="/news/run">
      <div class="searchrow" style="justify-content:space-between;">
        <div style="display:flex; gap:9px; align-items:center;">
          <button class="btn btn-primary" type="submit"
                  {% if state.running or cooldown_left %}disabled{% endif %}>
            {% if state.running %}Fetching...
            {% elif cooldown_left %}Wait {{ (cooldown_left // 60) }}:{{ '%02d' % (cooldown_left % 60) }}
            {% else %}Refresh news{% endif %}
          </button>
          {% if notify_status != 'off' %}
            <button class="btn btn-ghost" type="submit" formaction="/news/test-notify">Test alert</button>
          {% endif %}
        </div>
        <div class="counts">
          Auto-runs daily at <b>{{ news_time }}</b> &middot; {{ total_items }} stories in {{ groups|length }} sections
          {% if state.last_run %} &middot; last run {{ state.last_run }}{% endif %}
        </div>
      </div>
    </form>
  </div>

  {% if state.running %}
    <div class="banner">
      <span class="pulse"></span>
      <span>Fetching, summarising and sorting the latest AI news - this page refreshes every 4 seconds.</span>
    </div>
  {% endif %}

  {% if state.log %}
    <div class="log">{% for line in state.log %}{{ line }}
{% endfor %}</div>
  {% endif %}

  {% if groups %}
    <div class="jumpbar">
      {% for category, stories in groups %}
        <a href="#cat{{ loop.index0 }}">{{ category }}<span class="n">{{ stories|length }}</span></a>
      {% endfor %}
    </div>
  {% endif %}

  <div class="newswrap" style="margin-top:18px;">

    <div>
      {% if groups %}
        {% for category, stories in groups %}
          <div class="catblock cat-{{ loop.index0 }}" id="cat{{ loop.index0 }}">
            <div class="cathead">
              <span class="bar"></span>
              <h2>{{ category }}</h2>
              <span class="n">{{ stories|length }}</span>
              <a class="top" href="#">&uarr; top</a>
            </div>

            {% for item in stories %}
              <div class="card">
                <h3><a href="{{ item.url }}" target="_blank" rel="noopener noreferrer">{{ item.title }}</a></h3>
                <div class="meta">
                  <span class="sourcetag">{% if item.source %}{{ item.source }}{% else %}{{ item.url | domain }}{% endif %}</span>
                  {% if item.published %}<span>{{ item.published }}</span>{% endif %}
                </div>
                <p>{{ item.summary }}</p>
                <a class="cardlink" href="{{ item.url }}" target="_blank" rel="noopener noreferrer">{{ item.url }}</a>
                <div class="cardactions">
                  {% if item.news_id in saved_ids %}
                    <form class="inline" method="post" action="/news/unsave/{{ item.news_id }}">
                      <button class="btn btn-sm btn-star" type="submit">&#9733; Saved - remove</button>
                    </form>
                  {% else %}
                    <form class="inline" method="post" action="/news/save/{{ item.news_id }}">
                      <button class="btn btn-sm" type="submit">&#9734; Save for later</button>
                    </form>
                  {% endif %}
                  {% if notify_status != 'off' %}
                    <form class="inline" method="post" action="/news/push/{{ item.news_id }}">
                      <button class="btn btn-sm" type="submit" title="Send this story to your phone">&#128241; Send to phone</button>
                    </form>
                  {% endif %}
                </div>
              </div>
            {% endfor %}
          </div>
        {% endfor %}
      {% else %}
        <div class="panel empty">
          <div class="big">&#128240;</div>
          <p>No stories yet. Press <b>Refresh news</b> to fetch today's headlines.</p>
        </div>
      {% endif %}
    </div>

    <div class="savedcol">
      <div class="savedhead">
        <h2>Saved for later</h2>
        <div class="n">{{ saved_items|length }} article{% if saved_items|length != 1 %}s{% endif %}</div>
      </div>
      <div class="savedbody">
        {% if saved_items and notify_status != 'off' %}
          <form method="post" action="/news/push-saved" style="margin-bottom:12px;">
            <button class="btn btn-sm" type="submit" style="width:100%;">&#128241; Send all to phone</button>
          </form>
        {% endif %}
        <div class="savedscroll">
          {% if saved_items %}
            {% for item in saved_items %}
              <div class="savedcard">
                {% if item.category %}<span class="cattag">{{ item.category }}</span>{% endif %}
                <a href="{{ item.url }}" target="_blank" rel="noopener noreferrer"
                   style="margin-top:6px;">{{ item.title }}</a>
                <div class="meta" style="margin-bottom:8px;">
                  {% if item.source %}<span class="sourcetag">{{ item.source }}</span>{% endif %}
                  <span>saved {{ item.saved_at }}</span>
                </div>
                <div style="display:flex; gap:6px;">
                  <form class="inline" method="post" action="/news/unsave/{{ item.news_id }}">
                    <button class="btn btn-sm btn-ghost" type="submit">Remove</button>
                  </form>
                  {% if notify_status != 'off' %}
                    <form class="inline" method="post" action="/news/push/{{ item.news_id }}">
                      <button class="btn btn-sm btn-ghost" type="submit" title="Send to phone">&#128241;</button>
                    </form>
                  {% endif %}
                </div>
              </div>
            {% endfor %}
          {% else %}
            <p class="muted" style="font-size:13px; padding:14px 0;">Nothing saved yet. Use the &#9734; button on any story.</p>
          {% endif %}
        </div>
      </div>
    </div>

  </div>

</div>
</body>
</html>
"""

CRM_PAGE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Hire2o - CRM</title>
""" + STYLE + """
</head>
<body>
""" + HEADER + """
<div class="shell">

  <div class="crmwrap">

    <aside class="crmside">

    <div class="actionbox box-calls">
      <div class="boxhead">
        <span class="icon">&#128222;</span>
        <h2>Suggested next actions</h2>
        <span class="n">{{ boxes.actions|length + suggestions|length }}</span>
      </div>

      {% if boxes.actions or suggestions %}
        <div class="boxscroll">

          {% for a in boxes.actions %}
            <div class="boxitem act act-{{ a.status }}">
              <div style="display:flex; align-items:center; gap:5px; flex-wrap:wrap;">
                <i class="dot prio-{{ a.colour }}"
                   title="{{ a.colour | priolabel }} - {{ a.colour | priomeaning }}"></i>
                <span class="who">{{ a.who }}</span>
                {% if a.org %}<span class="org">&middot; {{ a.org }}</span>{% endif %}
                {% if a.status == 'today' %}
                  <span class="pill pill-soon">today</span>
                {% else %}
                  <span class="pill pill-cold">in {{ a.days }}d</span>
                {% endif %}
              </div>
              {% if a.task %}<div class="what">{{ a.task }}</div>{% endif %}
              <div class="when">&#128197; {{ a.shown }}</div>
            </div>
          {% endfor %}

          {% if boxes.actions and suggestions %}
            <div class="boxsub">From activity analysis</div>
          {% endif %}

          {% for s in suggestions %}
            <div class="sugg p-{{ s.priority }}">
              <div style="display:flex; align-items:center; gap:5px; flex-wrap:wrap;">
                <span class="who">{{ s.who }}</span>
                {% if s.org %}<span class="org">&middot; {{ s.org }}</span>{% endif %}
                <span class="pill pill-{{ s.priority }}">{{ s.priority }}</span>
                {% if s.stale %}<span class="pill pill-stale">notes changed</span>{% endif %}
                <form class="inline" method="post" action="/crm/done/{{ s.row }}"
                      style="margin-left:auto;">
                  <input type="hidden" name="sheet" value="{{ sheet }}">
                  <button class="btn btn-sm btn-ghost donebtn" type="submit"
                          title="Mark done and remove from this list">&#10003; Done</button>
                </form>
              </div>
              <div class="action">{{ s.action }}</div>
              {% if s.timing %}<div class="timing">&#8986; {{ s.timing }}</div>{% endif %}
            </div>
          {% endfor %}
        </div>
      {% elif boxes.followups %}
        <div class="boxscroll">
          {% for f in boxes.followups %}
            <div class="boxitem">
              <div>
                <span class="who">{{ f.who }}</span>
                {% if f.org %}<span class="org">&middot; {{ f.org }}</span>{% endif %}
                {% if f.overdue %}<span class="pill pill-over">{{ f.days_over }}d overdue</span>{% endif %}
              </div>
              <div class="what">{{ f.task }}</div>
              {% if f.due_raw %}<div class="when">Due {{ f.due_raw }}</div>{% endif %}
            </div>
          {% endfor %}
        </div>
      {% else %}
        <div class="boxempty">
          Nothing yet.<br>
          <span style="font-size:12px;">
            Pick a date in <b>{{ boxes.cols.date or 'column F' }}</b> on any row,
            or press <b>Analyse activity</b> to read the notes.
          </span>
        </div>
      {% endif %}

      <div class="boxnote" style="font-size:11px;">
        Today and later only, newest first &middot;
        <b>{{ boxes.cols.date or 'column F' }}</b> + <b>{{ boxes.cols.task or 'column G' }}</b>
      </div>

      <div class="boxnote">
        <form method="post" action="/crm/analyze" style="display:flex; align-items:center; gap:10px;">
          <input type="hidden" name="sheet" value="{{ sheet }}">
          <button class="btn btn-sm btn-primary" type="submit" {% if state.running %}disabled{% endif %}>
            {% if state.running %}Analysing...{% else %}&#10024; Analyse activity{% endif %}
          </button>
          <span>
            {% if state.last_run %}Last run {{ state.last_run }}
            {% elif boxes.cols.activity %}Reads <b>{{ boxes.cols.activity }}</b>
            {% else %}No activity column found{% endif %}
          </span>
        </form>
      </div>
    </div>

    <div class="actionbox box-stale">
      <div class="boxhead">
        <span class="icon">&#128228;</span>
        <h2>Reconnect - quiet {{ boxes.stale_days }}+ days</h2>
        <span class="n">{{ boxes.stale|length }}</span>
      </div>
      {% if boxes.stale %}
        <div class="boxscroll">
          {% for s in boxes.stale %}
            <div class="boxitem">
              <div>
                <span class="who">{{ s.who }}</span>
                {% if s.org %}<span class="org">&middot; {{ s.org }}</span>{% endif %}
                {% if s.days %}
                  <span class="pill pill-cold">{{ s.days }} days</span>
                {% else %}
                  <span class="pill pill-cold">never</span>
                {% endif %}
              </div>
              <div class="when">
                {% if s.last_raw %}
                  Last activity {{ s.last_raw }}
                  {% if s.last_line %}<br><span style="opacity:.75;">{{ s.last_line[:90] }}</span>{% endif %}
                {% else %}
                  No dated activity found
                {% endif %}
              </div>
            </div>
          {% endfor %}
        </div>
      {% else %}
        <div class="boxempty">
          Everyone contacted within {{ boxes.stale_days }} days.<br>
          {% if not boxes.cols.activity and not boxes.cols.last %}
            <span style="font-size:12px;">No activity or last-contact column found in this sheet.</span>
          {% endif %}
        </div>
      {% endif %}
      <div class="boxnote">
        Names from <b>{{ boxes.cols.name or '-' }}</b> &middot; dates read from
        <b>{{ boxes.cols.activity or boxes.cols.last or '-' }}</b>
      </div>
    </div>

    </aside>

    <div class="crmmain">

  {% if state.running %}
    <div class="banner">
      <span class="pulse"></span>
      <span>Reading activity notes and working out next steps.</span>
      <a class="btn btn-sm" style="margin-left:auto;"
         href="/crm?sheet={{ sheet }}{% if q %}&q={{ q }}{% endif %}">&#8634; Check progress</a>
    </div>
  {% endif %}

  {% if state.log %}
    <div class="log" style="height:120px;">{% for line in state.log %}{{ line }}
{% endfor %}</div>
  {% endif %}

  <div class="panel panel-pad" style="margin-top:20px;">
    <form method="get" action="/crm">
      <div class="searchrow">
        <div>
          <label class="fieldlabel" for="sheet">Worksheet</label>
          <select id="sheet" name="sheet" onchange="this.form.submit()">
            {% if sheets %}
              {% for s in sheets %}
                <option value="{{ s }}" {% if s == sheet %}selected{% endif %}>{{ s }}</option>
              {% endfor %}
            {% else %}
              <option value="{{ sheet }}">{{ sheet }}</option>
            {% endif %}
          </select>
        </div>
        <div>
          <label class="fieldlabel" for="q">Search all columns</label>
          <input type="text" id="q" name="q" value="{{ q }}" placeholder="name, company, stage...">
        </div>
        <div style="align-self:flex-end; display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
          <button class="btn" type="submit">Search</button>
          {% if q %}<a href="/crm?sheet={{ sheet }}" style="font-size:13px; color:var(--ink-soft);">Clear</a>{% endif %}
          <a class="btn btn-primary" href="/crm?sheet={{ sheet }}&add=1">+ Add row</a>
          <button class="btn" type="submit" formaction="/crm/backup" formmethod="post"
                  title="Copy results.xlsx to results_backup.xlsx now">&#128190; Back up now</button>
          {% if backup.exists %}
            <button class="btn btn-danger" type="submit" formaction="/crm/restore" formmethod="post"
                    onclick="return confirm('Replace results.xlsx with the backup from {{ backup.when }}?\\n\\nAnything saved since then will be lost.');">
              &#8634; Restore
            </button>
          {% endif %}
        </div>
      </div>
    </form>
  </div>

  <div class="sectionhead">
    <h2>{{ sheet }}</h2>
    <span class="counts">showing {{ rows|length }} of {{ total }} rows</span>
    <span class="legend">
      <span><i class="dot prio-red"></i>Hot</span>
      <span><i class="dot prio-yellow"></i>Warm</span>
      <span><i class="dot prio-green"></i>Room Temp</span>
      <span><i class="dot prio-blue"></i>Cold</span>
    </span>
    {% if hidden_count %}
      <span class="hiddennote">{{ hidden_count }} column{% if hidden_count != 1 %}s{% endif %} hidden</span>
    {% endif %}

    <span class="sortbar">
      <span class="sortlabel">Sort</span>
      {% for label, col in sortable %}
        {% set active = (sort == col) %}
        {% set nextdir = 'desc' if (active and direction == 'asc') else 'asc' %}
        <a class="sortbtn {% if active %}on{% endif %}"
           href="/crm?sheet={{ sheet }}{% if q %}&q={{ q }}{% endif %}&sort={{ col }}&dir={{ nextdir }}">
          {{ label }}{% if active %} {% if direction == 'asc' %}&uarr;{% else %}&darr;{% endif %}{% endif %}
        </a>
      {% endfor %}
      {% if sort %}
        <a class="sortbtn clear"
           href="/crm?sheet={{ sheet }}{% if q %}&q={{ q }}{% endif %}">clear</a>
      {% endif %}
    </span>
  </div>

  {% if columns %}
    <form id="crmsave" method="post" action="/crm/save">
      <input type="hidden" name="sheet" value="{{ sheet }}">
      <input type="hidden" name="q" value="{{ q }}">
      <input type="hidden" name="sort" value="{{ sort }}">
      <input type="hidden" name="dir" value="{{ direction }}">
      <input type="hidden" name="priocol" value="{{ prio_col }}">
    </form>

    {% if prio_note %}
      <div class="warnline">&#9888; {{ prio_note }}</div>
    {% endif %}

    <div class="savebar">
      <button class="btn btn-primary" form="crmsave" type="submit" id="savebtn">
        &#128190; Save sheet
      </button>
      <span class="savestate" id="savestate">No unsaved changes</span>
      {% if saved is not none %}
        <span class="savedflash">
          {% if saved %}Saved {{ saved }} cell{% if saved != 1 %}s{% endif %} to {{ sheet }}
          {% else %}Nothing had changed{% endif %}
        </span>
      {% endif %}
    </div>

    <div class="tablewrap">
      <table>
        <thead>
          <tr>
            {% for col_num, key, label in columns %}
              <th class="{% if wide_col and wide_col == key.lower() %}wide4{% elif name_col and col_num == name_col %}namecell{% endif %}">{{ label }}</th>
            {% endfor %}
          </tr>
        </thead>
        <tbody>

          {% if adding %}
            <tr class="addrow">
              <form method="post" action="/crm/add" id="addform">
                <input type="hidden" name="sheet" value="{{ sheet }}">
              </form>
              {% for col_num, key, label in columns %}
                <td class="{% if wide_col and wide_col == key.lower() %}wide4{% endif %}">
                  {% if wide_col and wide_col == key.lower() %}
                    <textarea form="addform" class="cellin" name="col_{{ col_num }}"
                              rows="3" cols="45" placeholder="{{ label }}..."></textarea>
                  {% elif date_col and col_num == date_col %}
                    <input form="addform" class="whenin" type="date" name="col_{{ col_num }}">
                  {% elif prio_col and col_num == prio_col %}
                    <input type="hidden" form="addform" name="col_{{ col_num }}" value="Cold">
                    <button type="button" class="prio prio-blue" data-colour="blue"
                            title="Cold - click to change"
                            onclick="cyclePriority(this)"></button>
                  {% elif task_col and col_num == task_col %}
                    <textarea form="addform" class="taskin" name="col_{{ col_num }}"
                              rows="3" cols="30" placeholder="{{ label }}..."></textarea>
                  {% elif col_num == sno_col %}
                    <input form="addform" type="text" name="col_{{ col_num }}"
                           value="{{ next_sno }}" placeholder="{{ label }}">
                  {% else %}
                    <input form="addform" type="text" name="col_{{ col_num }}" placeholder="{{ label }}">
                  {% endif %}
                </td>
              {% endfor %}
            </tr>
            <tr class="addrow">
              <td colspan="{{ columns|length }}" style="padding-top:0;">
                <button class="btn btn-primary btn-sm" form="addform" type="submit">Save new row</button>
                <a class="btn btn-ghost btn-sm" href="/crm?sheet={{ sheet }}">Cancel</a>
              </td>
            </tr>
          {% endif %}

          {% for row in rows %}
            <tr>
              {% for col_num, key, label in columns %}
                {% set value = row.get(key, '') %}
                {% if wide_col and wide_col == key.lower() %}
                  <td class="wide4">
                    <textarea class="cellin autogrow" form="crmsave"
                              name="cell_{{ row._row }}_{{ col_num }}"
                              rows="{{ value | rows_for(45, 3, 30) }}" cols="45"
                              placeholder="Add activity..."
                              oninput="autogrow(this); markDirty();">{{ value }}</textarea>
                  </td>
                {% elif date_col and col_num == date_col %}
                  {% set iso = value | iso_date %}
                  <td class="editable">
                    <input class="cellin whenin" type="date" form="crmsave"
                           name="cell_{{ row._row }}_{{ col_num }}" value="{{ iso }}"
                           onchange="markDirty()">
                    {% if value and not iso %}
                      <div class="rawnote" title="Could not read this as a date">{{ value }}</div>
                    {% endif %}
                  </td>
                {% elif prio_col and col_num == prio_col %}
                  {% set colour = value | prio %}
                  <td class="priocell">
                    <input type="hidden" form="crmsave"
                           name="cell_{{ row._row }}_{{ col_num }}"
                           value="{{ colour | priolabel }}">
                    <button type="button" class="prio prio-{{ colour }}"
                            data-colour="{{ colour }}"
                            title="{{ colour | priolabel }} - {{ colour | priomeaning }} - click to change"
                            onclick="cyclePriority(this)"></button>
                  </td>
                {% elif name_col and col_num == name_col %}
                  <td class="namecell">
                    {% if value %}
                      <button type="button" class="namelink clamp3"
                              title="{{ value }} - click for the journey"
                              onclick="openJourney({{ row._row }})">{{ value }}</button>
                    {% endif %}
                  </td>
                {% elif col_num in edit_cols %}
                  <td class="editable editcell">
                    <input class="cellin" type="text" form="crmsave"
                           name="cell_{{ row._row }}_{{ col_num }}" value="{{ value }}"
                           placeholder="{{ label }}..." oninput="markDirty()">
                  </td>
                {% elif task_col and col_num == task_col %}
                  <td class="editable taskcell">
                    <textarea class="cellin taskin" form="crmsave"
                              name="cell_{{ row._row }}_{{ col_num }}"
                              rows="3" cols="30"
                              placeholder="Action item..."
                              oninput="markDirty()">{{ value }}</textarea>
                  </td>
                {% else %}
                  <td>
                    {% if value.startswith('http://') or value.startswith('https://') %}
                      <a class="url" href="{{ value }}" target="_blank" rel="noopener noreferrer">{{ value }}</a>
                    {% elif '@' in value and ' ' not in value and '.' in value %}
                      <a href="mailto:{{ value }}">{{ value }}</a>
                    {% else %}
                      {{ value }}
                    {% endif %}
                  </td>
                {% endif %}
              {% endfor %}
            </tr>
          {% endfor %}

        </tbody>
      </table>
    </div>

    <div class="savebar savebar-bottom">
      <button class="btn btn-primary" form="crmsave" type="submit">&#128190; Save sheet</button>
      <span class="savestate" id="savestate2">No unsaved changes</span>
    </div>

    <p class="muted" style="font-size:12.5px; margin-top:14px;">
      <b>{{ workbook }}</b> &rarr; sheet <b>{{ sheet }}</b>.
      Every white box on this page is editable - press <b>Save sheet</b> and nothing is written until you do.
      Keep the file closed in Excel while using this page.
      {% if backup.exists %}
        <br>Backup last written <b>{{ backup.when }}</b>
        ({{ (backup.size / 1024) | round(1) }} KB) &middot; also taken automatically before every save.
      {% else %}
        <br>No backup yet - press <b>Back up now</b> to make one.
      {% endif %}
    </p>

  {% else %}
    <div class="panel empty">
      <div class="big">&#128202;</div>
      <p>
        No worksheet named <b>{{ sheet }}</b> found in {{ workbook }} - or it has no header row.<br>
        Add column names in row 1, save and close Excel, then reload.
      </p>
    </div>
  {% endif %}

    </div><!-- /crmmain -->
  </div><!-- /crmwrap -->

  <div class="jmodal" id="jmodal" onclick="if(event.target===this){closeJourney();}">
    <div class="jcard" role="dialog" aria-label="Relationship journey">
      <div class="jhead">
        <div>
          <h2 id="jname">&nbsp;</h2>
          <div class="jsub" id="jcompany"></div>
        </div>
        <button type="button" class="jclose" onclick="closeJourney()" aria-label="Close">&times;</button>
      </div>
      <div class="jbody" id="jbody"></div>
    </div>
  </div>

  <script>
    function autogrow(el) {
      el.style.height = 'auto';
      el.style.height = (el.scrollHeight + 2) + 'px';
    }

    var dirty = false;

    // click order, and the word written into the spreadsheet for each colour
    var PRIORITY = [['blue', 'Cold'], ['red', 'Hot'], ['yellow', 'Warm'], ['green', 'Room Temp']];

    function cyclePriority(btn) {
      var now = btn.getAttribute('data-colour') || 'blue';
      var at = 0;
      for (var i = 0; i < PRIORITY.length; i++) { if (PRIORITY[i][0] === now) { at = i; } }
      var next = PRIORITY[(at + 1) % PRIORITY.length];

      var hidden = btn.parentNode.querySelector('input[type=hidden]');
      if (hidden) { hidden.value = next[1]; }
      btn.setAttribute('data-colour', next[0]);
      btn.className = 'prio prio-' + next[0];
      btn.title = next[1] + ' - click to change';
      markDirty();
    }

    function markDirty(el) {
      dirty = true;
      ['savestate', 'savestate2'].forEach(function (id) {
        var s = document.getElementById(id);
        if (s) { s.textContent = 'Unsaved changes'; s.classList.add('dirty'); }
      });
    }

    document.addEventListener('DOMContentLoaded', function () {
      document.querySelectorAll('textarea.autogrow').forEach(autogrow);

      // highlight only the cells actually changed, so it is obvious what will be written
      document.querySelectorAll('#crmsave, [form=crmsave]').forEach(function (el) {
        if (!el.name) { return; }
        el.addEventListener('input', function () {
          el.classList.toggle('touched', el.value !== el.defaultValue);
        });
        el.addEventListener('change', function () {
          el.classList.toggle('touched', el.value !== el.defaultValue);
        });
      });

      var form = document.getElementById('crmsave');
      if (form) { form.addEventListener('submit', function () { dirty = false; }); }

      window.addEventListener('beforeunload', function (e) {
        if (dirty) { e.preventDefault(); e.returnValue = ''; }
      });
    });

    // ---- relationship journey ------------------------------------------------
    var J_COLOUR = {
      'No response': '#8b9099', 'Contact': '#6c5ce7',
      'Progress': '#16a34a', 'In person': '#0f2f5c', 'Next step': '#f59e0b'
    };
    var J_BANDS = ['In person', 'Progress', 'Contact', 'No response'];
    var SVGNS = 'http://www.w3.org/2000/svg';

    function el(tag, attrs, parent) {
      var node = document.createElementNS(SVGNS, tag);
      for (var k in attrs) { node.setAttribute(k, attrs[k]); }
      if (parent) { parent.appendChild(node); }
      return node;
    }

    function marker(parent, channel, x, y, colour, hollow) {
      var fill = hollow ? '#ffffff' : colour;
      var common = { fill: fill, stroke: colour, 'stroke-width': hollow ? 2.5 : 1 };
      if (channel === 'WhatsApp') {
        return el('circle', Object.assign({ cx: x, cy: y, r: 7 }, common), parent);
      }
      if (channel === 'call') {
        return el('polygon', Object.assign({
          points: [x, y - 8, x + 7.5, y + 6, x - 7.5, y + 6].join(' ') }, common), parent);
      }
      if (channel === 'in person') {
        var pts = [];
        for (var i = 0; i < 10; i++) {
          var r = i % 2 ? 4 : 9.5, a = Math.PI / 5 * i - Math.PI / 2;
          pts.push((x + r * Math.cos(a)).toFixed(1), (y + r * Math.sin(a)).toFixed(1));
        }
        return el('polygon', Object.assign({ points: pts.join(' ') }, common), parent);
      }
      return el('rect', Object.assign({ x: x - 6, y: y - 6, width: 12, height: 12, rx: 2.5 }, common), parent);
    }

    function tipOn(node, html, tip) {
      node.style.cursor = 'pointer';
      node.addEventListener('mousemove', function (e) {
        tip.innerHTML = html;
        tip.style.display = 'block';
        var box = tip.parentNode.getBoundingClientRect();
        var left = e.clientX - box.left + 14;
        tip.style.left = Math.min(left, box.width - tip.offsetWidth - 10) + 'px';
        tip.style.top = (e.clientY - box.top - tip.offsetHeight - 12) + 'px';
      });
      node.addEventListener('mouseleave', function () { tip.style.display = 'none'; });
    }

    function esc(s) {
      return String(s).replace(/[&<>"]/g, function (c) {
        return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
      });
    }

    function niceDate(iso) {
      var d = new Date(iso + 'T00:00:00');
      return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
    }

    function drawJourney(data, host) {
      host.innerHTML = '';
      var tip = document.createElement('div');
      tip.className = 'jtip';
      host.appendChild(tip);

      var pts = data.points || [];
      if (!pts.length) {
        host.insertAdjacentHTML('beforeend',
          '<p class="jempty">No dated entries in the Activity column for this row.' +
          '<br><span>Lines need to start with a date, like <b>Sep 23 - called, will introduce me</b>.</span></p>');
        return;
      }

      // ---- stat tiles
      var s = data.summary;
      var tiles = [['Touchpoints', s.touchpoints], ['No response', s.no_response],
                   ['Days to breakthrough', s.days === null ? '--' : s.days],
                   ['Progress moments', s.progress]];
      host.insertAdjacentHTML('beforeend', '<div class="jstats">' + tiles.map(function (t) {
        return '<div class="jstat"><span>' + t[0] + '</span><b>' + t[1] + '</b></div>';
      }).join('') + '</div>');

      // ---- legend
      var swatches = J_BANDS.concat(['Next step']).map(function (k) {
        return '<span><i style="background:' + J_COLOUR[k] + '"></i>' + k + '</span>';
      }).join('');
      host.insertAdjacentHTML('beforeend',
        '<div class="jlegend">' + swatches +
        '<span class="jshapes">Shapes: &#9679; WhatsApp &nbsp;&#9650; call &nbsp;&#9632; message &nbsp;&#9733; in person</span></div>');

      // ---- step chart
      var W = 920, H = 320, L = 110, R = 46, T = 18, B = 44;
      var svg = el('svg', { viewBox: '0 0 ' + W + ' ' + H, class: 'jsvg' }, host);
      var t0 = new Date(pts[0].date + 'T00:00:00').getTime();
      var t1 = new Date(pts[pts.length - 1].date + 'T00:00:00').getTime();
      if (t1 === t0) { t1 = t0 + 86400000 * 30; }
      var pad = (t1 - t0) * 0.04;
      function X(iso) {
        var t = new Date(iso + 'T00:00:00').getTime();
        return L + (t - (t0 - pad)) / ((t1 + pad) - (t0 - pad)) * (W - L - R);
      }
      var bandH = (H - T - B) / (J_BANDS.length - 1);
      function Y(level) {
        var band = level === 'Next step' ? 'Progress' : level;
        var i = J_BANDS.indexOf(band);
        return T + (i < 0 ? J_BANDS.length - 1 : i) * bandH;
      }

      J_BANDS.forEach(function (name) {
        el('line', { x1: L, x2: W - R, y1: Y(name), y2: Y(name),
                     stroke: '#e3e8ef', 'stroke-width': 1 }, svg);
        var label = el('text', { x: L - 14, y: Y(name) + 4, 'text-anchor': 'end',
                                 class: 'jaxis' }, svg);
        label.textContent = name;
      });

      var d = 'M' + X(pts[0].date) + ' ' + Y(pts[0].level);
      for (var i = 1; i < pts.length; i++) {
        d += ' L' + X(pts[i].date) + ' ' + Y(pts[i - 1].level) +
             ' L' + X(pts[i].date) + ' ' + Y(pts[i].level);
      }
      el('path', { d: d, fill: 'none', stroke: '#b9c2cf', 'stroke-width': 1.6 }, svg);

      pts.forEach(function (p) {
        var node = marker(svg, p.channel, X(p.date), Y(p.level), J_COLOUR[p.level] || '#6c5ce7',
                          p.level === 'Next step');
        tipOn(node, '<b>' + niceDate(p.date) + '</b><span class="jpill" style="background:' +
                    (J_COLOUR[p.level] || '#6c5ce7') + '">' + p.level + '</span><br>' +
                    esc(p.note), tip);
      });

      // month ticks
      var seen = {};
      (data.months || []).forEach(function (m) {
        var iso = m.key + '-15';
        if (seen[m.key]) { return; }
        seen[m.key] = 1;
        var tx = Math.max(L, Math.min(X(iso), W - 6));
        var tick = el('text', { x: tx, y: H - 16, class: 'jaxis',
                                'text-anchor': tx >= W - 6 ? 'end' : 'middle' }, svg);
        tick.textContent = m.label;
      });
      el('line', { x1: L, x2: W - R, y1: H - B + 14, y2: H - B + 14,
                   stroke: '#e3e8ef', 'stroke-width': 1 }, svg);

      // ---- monthly bars
      var months = data.months || [];
      host.insertAdjacentHTML('beforeend', '<h3 class="jh3">Touchpoints per month</h3>');
      var BH = 210, BB = 34, BT = 10;
      var bars = el('svg', { viewBox: '0 0 ' + W + ' ' + BH, class: 'jsvg' }, host);
      var top = 0;
      months.forEach(function (m) {
        var total = 0;
        for (var k in m.counts) { total += m.counts[k]; }
        top = Math.max(top, total);
      });
      top = Math.max(top, 1);
      var plot = BH - BB - BT;
      for (var g = 0; g <= top; g++) {
        var gy = BT + plot - (g / top) * plot;
        el('line', { x1: L, x2: W - R, y1: gy, y2: gy, stroke: '#eef1f6' }, bars);
        var gl = el('text', { x: L - 14, y: gy + 4, 'text-anchor': 'end', class: 'jaxis' }, bars);
        gl.textContent = g;
      }
      var slot = (W - L - R) / months.length;
      var barW = Math.min(34, slot * 0.46);
      months.forEach(function (m, mi) {
        var cx = L + slot * (mi + 0.5);
        var y = BT + plot;
        ['No response', 'Contact', 'Progress', 'In person', 'Next step'].forEach(function (level) {
          var n = m.counts[level] || 0;
          if (!n) { return; }
          var h = (n / top) * plot;
          y -= h;
          var rect = el('rect', { x: cx - barW / 2, y: y, width: barW, height: h,
                                  fill: J_COLOUR[level], rx: 2 }, bars);
          tipOn(rect, '<b>' + m.label + ' ' + m.year + '</b><br>' + n + ' x ' + level, tip);
        });
        var lab = el('text', { x: cx, y: BH - 12, 'text-anchor': 'middle', class: 'jaxis' }, bars);
        lab.textContent = m.label;
      });
    }

    function openJourney(row) {
      var modal = document.getElementById('jmodal');
      var body = document.getElementById('jbody');
      document.getElementById('jname').textContent = 'Loading...';
      document.getElementById('jcompany').textContent = '';
      body.innerHTML = '<p class="jempty">Reading the activity notes...</p>';
      modal.classList.add('open');

      var sheet = document.querySelector('#crmsave input[name=sheet]');
      fetch('/crm/journey?sheet=' + encodeURIComponent(sheet ? sheet.value : '') + '&row=' + row)
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (data.error) {
            body.innerHTML = '<p class="jempty">' + esc(data.error) + '</p>';
            return;
          }
          document.getElementById('jname').textContent = data.name;
          document.getElementById('jcompany').textContent = data.company;
          drawJourney(data, body);
        })
        .catch(function (err) {
          body.innerHTML = '<p class="jempty">Could not load the journey: ' + esc(err) + '</p>';
        });
    }

    function closeJourney() {
      document.getElementById('jmodal').classList.remove('open');
    }

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') { closeJourney(); }
    });
  </script>

</div>
</body>
</html>
"""


LINKEDIN_PAGE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Hire2o - LinkedIn Post</title>
  {% if state.running %}<meta http-equiv="refresh" content="4">{% endif %}
""" + STYLE + """
</head>
<body>
""" + HEADER + """
<div class="shell">

  <div class="panel panel-pad searchbar">
    <form method="post" action="/linkedin/run">
      <div class="searchrow" style="justify-content:space-between;">
        <div style="display:flex; gap:9px; align-items:center;">
          <button class="btn btn-primary" type="submit" {% if state.running %}disabled{% endif %}>
            {% if state.running %}Writing...{% else %}&#10024; Generate today's post{% endif %}
          </button>
          {% if post and notify_status != 'off' %}
            <button class="btn btn-ghost" type="submit" formaction="/linkedin/push">
              &#128241; Send to phone
            </button>
          {% endif %}
        </div>
        <div class="counts">
          Runs automatically at <b>{{ post_time }}</b> ET daily
          {% if post %} &middot; today's post dated <b>{{ post.date }}</b>{% endif %}
        </div>
      </div>
    </form>
  </div>

  {% if state.running %}
    <div class="banner">
      <span class="pulse"></span>
      <span>Finding an article, writing the post and rendering the image - this page refreshes every 4 seconds.</span>
    </div>
  {% endif %}

  {% if state.log %}
    <div class="log" style="height:130px;">{% for line in state.log %}{{ line }}
{% endfor %}</div>
  {% endif %}

  {% if post %}
    <div class="liwrap">

      <div class="libox">
        <div class="boxhead">
          <span class="icon">&#128240;</span>
          <h2>1 &middot; The article</h2>
          <span class="n">{{ post.source or 'source' }}</span>
        </div>
        <div class="libody">
          <p class="arttitle">
            <a href="{{ post.url }}" target="_blank" rel="noopener noreferrer">{{ post.title }}</a>
          </p>
          {% if post.summary %}<p class="artsum">{{ post.summary }}</p>{% endif %}
          <div class="meta" style="margin-bottom:12px;">
            {% if post.source %}<span class="sourcetag">{{ post.source }}</span>{% endif %}
            {% if post.published %}<span>{{ post.published }}</span>{% endif %}
          </div>
          <a class="cardlink" href="{{ post.url }}" target="_blank" rel="noopener noreferrer">{{ post.url }}</a>
          <div style="margin-top:14px; display:flex; gap:8px;">
            <a class="btn btn-sm" href="{{ post.url }}" target="_blank" rel="noopener noreferrer">
              Read the article &#8599;
            </a>
            <button class="btn btn-sm copybtn" type="button"
                    onclick="copyText(this, {{ post.url | tojson }})">Copy link</button>
          </div>
        </div>
      </div>

      <div class="libox">
        <div class="boxhead">
          <span class="icon">&#9997;</span>
          <h2>2 &middot; Your post</h2>
          {% set wc = post.post.split() | length %}
          <span class="n {% if wc <= 50 %}wc-ok{% else %}wc-off{% endif %}">{{ wc }} words</span>
        </div>
        <div class="libody">
          {% if post.post %}
            <div class="posttext" id="posttext">{{ post.post }}</div>
            <div style="margin-top:14px; display:flex; gap:8px; flex-wrap:wrap;">
              <button class="btn btn-sm btn-primary copybtn" type="button"
                      onclick="copyText(this, {{ post.post | tojson }})">Copy post text</button>
              <a class="btn btn-sm" href="https://www.linkedin.com/feed/" target="_blank"
                 rel="noopener noreferrer">Open LinkedIn &#8599;</a>
            </div>
          {% else %}
            <p class="muted">No post text - the model returned nothing. Press
            <b>Generate today's post</b> to try again.</p>
          {% endif %}
        </div>
      </div>

      <div class="libox">
        <div class="boxhead">
          <span class="icon">&#127912;</span>
          <h2>3 &middot; The image</h2>
          <span class="n">1200 &times; 628</span>
        </div>
        <div class="libody">
          {% if post.image_file %}
            <img class="shareimg"
                 src="{{ url_for('static', filename='posts/' ~ post.image_file) }}"
                 alt="Share image">
            <p class="imgnote">
              Right-click &rarr; <b>Save image</b>, then attach it to your LinkedIn post.
              Sized for LinkedIn's link-preview ratio.
            </p>
            <div style="margin-top:10px; display:flex; gap:8px;">
              <a class="btn btn-sm"
                 href="{{ url_for('static', filename='posts/' ~ post.image_file) }}"
                 download>&#11015; Download image</a>
              <a class="btn btn-sm btn-ghost"
                 href="{{ url_for('static', filename='posts/' ~ post.image_file) }}"
                 target="_blank" rel="noopener noreferrer">Open full size &#8599;</a>
            </div>
          {% else %}
            <p class="muted">No image was generated for this post.</p>
          {% endif %}
        </div>
      </div>

      <div class="libox">
        <div class="boxhead">
          <span class="icon">&#128338;</span>
          <h2>4 &middot; Earlier posts</h2>
          <span class="n">{{ past|length }}</span>
        </div>
        <div class="libody" style="padding-top:6px;">
          {% if past %}
            <div style="max-height:320px; overflow-y:auto;">
              {% for p in past %}
                <div class="pastrow">
                  <span class="d">{{ p.date }}</span>
                  <span>
                    <a href="{{ p.url }}" target="_blank" rel="noopener noreferrer">{{ p.title[:78] }}</a>
                    {% if p.post %}
                      <button class="btn btn-sm btn-ghost copybtn" type="button"
                              style="margin-left:6px;"
                              onclick="copyText(this, {{ p.post | tojson }})">copy</button>
                    {% endif %}
                  </span>
                </div>
              {% endfor %}
            </div>
          {% else %}
            <p class="muted" style="font-size:13px;">Nothing yet. Today's is your first.</p>
          {% endif %}
        </div>
      </div>

    </div>

  {% else %}
    <div class="panel empty">
      <div class="big">&#128221;</div>
      <p>
        No post yet for today.<br>
        Press <b>Generate today's post</b> to find an AI article and draft something from it.
      </p>
    </div>
  {% endif %}

  <div class="sectionhead" style="margin-top:34px;">
    <h2>Posts to comment on</h2>
    <span class="counts">
      showing {{ engage|length }}{% if engage_total > engage|length %} of {{ engage_total }} saved{% endif %}
      &middot; last {{ engage_days }} days
    </span>
  </div>

  <div class="panel panel-pad">
    <form method="post" action="/linkedin/add">
      <label class="fieldlabel" for="libulk">Paste LinkedIn posts</label>
      <textarea class="pastebox" id="libulk" name="bulk"
                placeholder="https://www.linkedin.com/posts/someone_slug-activity-7123456789-abcd
The text of their post goes on the lines below the link.

https://www.linkedin.com/feed/update/urn:li:activity:7987654321
And another post here."></textarea>
      <div style="display:flex; align-items:center; gap:10px; margin-top:11px; flex-wrap:wrap;">
        <button class="btn btn-primary" type="submit">Add posts</button>
        <span class="pastehint" style="margin:0;">
          One post per link &mdash; text goes on the lines below it.
          Comments are drafted as soon as you add them.
        </span>
      </div>
    </form>
  </div>

  {% if engage %}
    <form method="post" action="/linkedin/draft" style="margin:14px 0;">
      <button class="btn btn-sm" type="submit" {% if state.running %}disabled{% endif %}>
        &#10024; Draft missing comments
      </button>
      <button class="btn btn-sm btn-ghost" type="submit" name="force" value="1"
              {% if state.running %}disabled{% endif %}>Redraft all</button>
    </form>

    {% for p in engage %}
      <div class="xpost {% if p.done %}done{% endif %}">
        <div class="xhead">
          {% if p.who %}
            <span class="xauthor">{{ p.who }}</span>
          {% else %}
            <span class="xauthor nohandle">no author &mdash; paste the post link too</span>
          {% endif %}
          <span class="xage">added {{ p.added_at }}</span>
        </div>

        <div class="xtext">{{ p.text }}</div>

        {% if p.url %}
          <a class="xlink" href="{{ p.url }}" target="_blank" rel="noopener noreferrer">{{ p.url }}</a>
        {% endif %}

        {% if p.comment %}
          <div class="xreply">
            <div class="lbl">
              Suggested comment{% if p.who %} on <span class="toh">{{ p.who }}</span>'s post{% endif %}
            </div>
            <div class="body">{{ p.comment }}</div>
            <div class="wc">{{ p.comment.split()|length }} words</div>
          </div>
        {% elif state.running %}
          <div class="xwait"><span class="pulse"></span> Drafting your comment&hellip;</div>
        {% else %}
          <div class="xnone">No comment drafted yet &mdash; press <b>Draft missing comments</b>.</div>
        {% endif %}

        <div class="xactions">
          {% if p.url %}
            <a class="btn btn-sm btn-primary" href="{{ p.url }}"
               target="_blank" rel="noopener noreferrer">&#128279; Go to post</a>
          {% endif %}
          {% if p.comment %}
            <button class="btn btn-sm copybtn" type="button"
                    onclick="copyText(this, {{ p.comment | tojson }})">&#128203; Copy comment</button>
          {% endif %}

          <form class="inline" method="post" action="/linkedin/commented/{{ p.post_id }}">
            <label class="donecheck {% if p.done %}on{% endif %}">
              <input type="checkbox" {% if p.done %}checked{% endif %}
                     onchange="this.form.submit()">
              {% if p.done %}Commented{% else %}I have commented{% endif %}
            </label>
          </form>

          <form class="inline" method="post" action="/linkedin/redraft/{{ p.post_id }}"
                style="margin-left:auto;">
            <button class="btn btn-sm btn-ghost" type="submit">&#8635; Redraft</button>
          </form>
          <form class="inline" method="post" action="/linkedin/remove/{{ p.post_id }}"
                onsubmit="return confirm('Delete this post?');">
            <button class="btn btn-sm btn-trash" type="submit">&#128465; Delete</button>
          </form>
        </div>
      </div>
    {% endfor %}
  {% else %}
    <div class="panel empty" style="margin-top:14px;">
      <div class="big">&#128172;</div>
      <p>Nothing to comment on yet. Paste a LinkedIn post above.</p>
    </div>
  {% endif %}

  <script>
    function copyText(btn, text) {
      navigator.clipboard.writeText(text).then(function () {
        var was = btn.textContent;
        btn.textContent = 'Copied';
        btn.classList.add('done');
        setTimeout(function () { btn.textContent = was; btn.classList.remove('done'); }, 1600);
      });
    }
  </script>

</div>
</body>
</html>
"""


YOUTUBE_PAGE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Hire2o - YouTube</title>
  {% if state.running %}<meta http-equiv="refresh" content="4">{% endif %}
""" + STYLE + """
</head>
<body>
""" + HEADER + """
<div class="shell">

  <div class="panel panel-pad searchbar">
    <form method="post" action="/youtube/run">
      <div class="searchrow" style="justify-content:space-between;">
        <div style="display:flex; gap:9px; align-items:center;">
          <button class="btn btn-primary" type="submit" {% if state.running %}disabled{% endif %}>
            {% if state.running %}Refreshing...{% else %}&#8635; Refresh stats{% endif %}
          </button>
          <a class="btn btn-ghost" href="{{ channel_url }}" target="_blank" rel="noopener noreferrer">
            Open channel &#8599;
          </a>
        </div>
        <div class="counts">
          Auto-refreshes daily at <b>{{ refresh_time }}</b>
          {% if state.last_run %} &middot; last run {{ state.last_run }}
          {% elif chan.fetched_at %} &middot; data from {{ chan.fetched_at }}{% endif %}
        </div>
      </div>
    </form>
  </div>

  {% if state.running %}
    <div class="banner">
      <span class="pulse"></span>
      <span>Reading the channel and every video - this page refreshes every 4 seconds.</span>
    </div>
  {% endif %}

  {% if state.log %}
    <div class="log" style="height:130px;">{% for line in state.log %}{{ line }}
{% endfor %}</div>
  {% endif %}

  {% if yt_status != 'ready' %}
    <div class="setupbox">
      <h3>&#128273; One-time setup - YouTube API key ({{ yt_status }})</h3>
      <ol>
        <li>Go to <b>console.cloud.google.com</b> and sign in with your Google account.</li>
        <li>Create a project (any name), then open <b>APIs &amp; Services &rarr; Library</b>.</li>
        <li>Search <b>YouTube Data API v3</b> and press <b>Enable</b>.</li>
        <li>Open <b>Credentials &rarr; Create credentials &rarr; API key</b>, and copy the key.</li>
        <li>Add this line to your <code>.env</code> file, then restart the app:<br>
            <code>YOUTUBE_API_KEY=AIza...your-key...</code></li>
      </ol>
      <p style="margin:12px 0 0; font-size:12.5px;">
        Free, no card needed. A full refresh costs about 3 units of the 10,000 daily quota.
      </p>
    </div>
  {% endif %}

  {% if chan.title %}
    <div class="chanbar">
      {% if chan.thumbnail %}<img src="{{ chan.thumbnail }}" alt="">{% endif %}
      <div>
        <div class="nm">{{ chan.title }}</div>
        <div class="hd">
          {% if chan.handle %}{{ chan.handle }} &middot; {% endif %}
          {{ '{:,}'.format(chan.subscribers|int) }} subscribers &middot;
          {{ chan.video_count }} videos
        </div>
      </div>
      <a class="btn btn-sm" style="margin-left:auto;" href="{{ channel_url }}"
         target="_blank" rel="noopener noreferrer">View on YouTube &#8599;</a>
    </div>
  {% endif %}

  {% if rows %}
    <div class="tilerow">
      <div class="tile accent">
        <div class="k">Subscribers</div>
        <div class="v">{{ '{:,}'.format(chan.subscribers|int) if chan.subscribers else '-' }}</div>
        {% if chan.video_count %}<div class="s">channel total</div>{% endif %}
      </div>
      <div class="tile">
        <div class="k">Videos</div>
        <div class="v">{{ totals.videos }}</div>
      </div>
      <div class="tile">
        <div class="k">Total views</div>
        <div class="v">{{ totals.views_fmt }}</div>
        <div class="s">{{ totals.avg_views }} average</div>
      </div>
      <div class="tile">
        <div class="k">Likes</div>
        <div class="v">{{ totals.likes_fmt }}</div>
      </div>
      <div class="tile">
        <div class="k">Comments</div>
        <div class="v">{{ totals.comments_fmt }}</div>
      </div>
      <div class="tile">
        <div class="k">Engagement</div>
        <div class="v">{{ totals.engagement }}%</div>
        <div class="s">likes + comments / views</div>
      </div>
    </div>

    <div class="sectionhead">
      <h2>Videos</h2>
      <span class="counts">{{ rows|length }} published &middot; click a column to sort</span>
    </div>

    <div class="tablewrap">
      <table>
        <thead>
          <tr>
            <th style="width:148px;"></th>
            {% set cols = [('title','Video','left'), ('published','Published','left'),
                           ('views','Views','right'), ('likes','Likes','right'),
                           ('comments','Comments','right'), ('engagement','Engagement','right')] %}
            {% for key, label, align in cols %}
              {% set active = (sort == key) %}
              {% set nextdir = 'asc' if (active and direction == 'desc') else 'desc' %}
              <th class="sortable {% if active %}on{% endif %}"
                  style="text-align:{{ align }};">
                <a href="/youtube?sort={{ key }}&dir={{ nextdir }}">
                  {{ label }}{% if active %} {% if direction == 'desc' %}&darr;{% else %}&uarr;{% endif %}{% endif %}
                </a>
              </th>
            {% endfor %}
          </tr>
        </thead>
        <tbody>
          {% for v in rows %}
            <tr>
              <td>
                <a href="{{ v.url }}" target="_blank" rel="noopener noreferrer">
                  {% if v.thumbnail %}<img class="thumb" src="{{ v.thumbnail }}" alt="">{% endif %}
                </a>
              </td>
              <td style="min-width:300px; max-width:430px;">
                <a class="vidtitle" href="{{ v.url }}" target="_blank" rel="noopener noreferrer">{{ v.title }}</a>
                <div class="vidmeta">
                  {% if v.duration %}{{ v.duration }}{% endif %}
                  {% if v.privacy and v.privacy != 'public' %}
                    &middot; <span class="pill pill-cold">{{ v.privacy }}</span>
                  {% endif %}
                  {% if v.views_per_day %} &middot; {{ v.views_per_day }} views/day{% endif %}
                </div>
              </td>
              <td class="datecell">
                {{ v.published }}
                {% if v.age_days is not none %}
                  <div class="vidmeta">{{ v.age_days }}d ago</div>
                {% endif %}
              </td>
              <td class="num"><b>{{ v.views_fmt }}</b></td>
              <td class="num">{{ v.likes_fmt }}</td>
              <td class="num">{{ v.comments_fmt }}</td>
              <td class="num">
                <span class="engage {% if v.engagement >= 5 %}eng-hi{% elif v.engagement >= 2 %}eng-mid{% else %}eng-lo{% endif %}">
                  {{ v.engagement }}%
                </span>
              </td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>

    <p class="muted" style="font-size:12.5px; margin-top:14px;">
      Cached in <b>results.xlsx</b> on the <b>YouTubeVideos</b> sheet.
      Engagement is (likes + comments) &divide; views - green above 5%, amber above 2%.
    </p>

  {% elif yt_status == 'ready' %}
    <div class="panel empty">
      <div class="big">&#127909;</div>
      <p>No videos cached yet. Press <b>Refresh stats</b> to pull them from YouTube.</p>
    </div>
  {% endif %}

</div>
</body>
</html>
"""


X_PAGE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Hire2o - X</title>
  {% if state.running %}<meta http-equiv="refresh" content="4">{% endif %}
""" + STYLE + """
</head>
<body>
""" + HEADER + """
<div class="shell">

  <div class="panel panel-pad searchbar">
    <form method="post" action="/x/run">
      <div class="searchrow" style="justify-content:space-between;">
        <div style="display:flex; gap:9px; align-items:center;">
          <button class="btn btn-primary" type="submit" {% if state.running %}disabled{% endif %}>
            {% if state.running %}Working...{% else %}&#10024; Draft comments{% endif %}
          </button>
          <button class="btn btn-ghost" type="submit" name="force" value="1"
                  {% if state.running %}disabled{% endif %}>Redraft all</button>
          <a class="btn btn-ghost" href="https://x.com/{{ handle }}"
             target="_blank" rel="noopener noreferrer">@{{ handle }} &#8599;</a>
        </div>
        <div class="counts">
          {{ x_status }} &middot; last {{ max_age }} days &middot; top {{ top_n }}
          {% if state.last_run %} &middot; run {{ state.last_run }}{% endif %}
        </div>
      </div>
    </form>
  </div>

  {% if state.running %}
    <div class="banner">
      <span class="pulse"></span>
      <span>
        <b>Working on it.</b> Reading each post and drafting a reply &mdash;
        this usually takes 10&ndash;30 seconds per post. The page refreshes
        itself every 4 seconds, so just leave it open.
      </span>
    </div>
  {% endif %}

  {% if state.log %}
    <div class="log" style="height:120px;">{% for line in state.log %}{{ line }}
{% endfor %}</div>
  {% endif %}

  <div class="panel panel-pad" style="margin-top:18px;">
    <form method="post" action="/x/add">
      <label class="fieldlabel" for="bulk">Paste posts</label>
      <textarea class="pastebox" id="bulk" name="bulk"
                placeholder="https://x.com/someone/status/123456
The text of their post goes on the next lines.

https://x.com/another/status/789012
And another post here, separated by a blank line."></textarea>
      <div style="display:flex; align-items:flex-start; gap:12px; margin-top:11px;">
        <button class="btn btn-primary" type="submit">Add posts</button>
        <p class="pastehint" style="margin:0;">
          One post per block, separated by a blank line. Put the link on its own
          line - the handle is read from it. Replies are drafted as soon as you add them.
        </p>
      </div>
    </form>
  </div>

  {% if not api_ready %}
    <div class="setupbox">
      <h3>&#128274; Connect your timeline (optional)</h3>
      <p style="margin:0 0 10px; font-size:13.5px;">
        Pulling posts automatically from the accounts you follow needs a paid X
        tier and an OAuth user token. Until then, paste posts above.
      </p>
      <ol>
        {% for step in setup %}<li>{{ step }}</li>{% endfor %}
      </ol>
    </div>
  {% endif %}

  <div class="sectionhead">
    <h2>Posts to reply to</h2>
    <span class="counts">
      showing {{ posts|length }}{% if total > posts|length %} of {{ total }} saved{% endif %}
    </span>
  </div>

  {% if posts %}
    {% for p in posts %}
      <div class="xpost {% if p.done %}done{% endif %}">
        <div class="xhead">
          {% if p.author %}
            <a class="xauthor" href="https://x.com/{{ p.author }}"
               target="_blank" rel="noopener noreferrer">@{{ p.author }}</a>
          {% else %}
            <span class="xauthor nohandle">no handle &mdash; paste the post link too</span>
          {% endif %}
          {% if p.age %}<span class="xage">{{ p.age }}</span>{% endif %}
          {% if p.likes or p.reposts or p.replies %}
            <span class="xmetrics">
              {% if p.likes %}<span>&#9825; {{ p.likes }}</span>{% endif %}
              {% if p.reposts %}<span>&#8644; {{ p.reposts }}</span>{% endif %}
              {% if p.replies %}<span>&#128172; {{ p.replies }}</span>{% endif %}
            </span>
          {% endif %}
        </div>

        <div class="xtext">{{ p.text }}</div>

        {% if p.url %}
          <a class="xlink" href="{{ p.url }}" target="_blank" rel="noopener noreferrer">{{ p.url }}</a>
        {% endif %}

        {% if p.comment %}
          <div class="xreply">
            <div class="lbl">
              Suggested reply{% if p.author %} to <span class="toh">@{{ p.author }}</span>{% endif %}
            </div>
            <div class="body">{{ p.comment }}</div>
            <div class="wc">{{ p.comment.split()|length }} words</div>
          </div>
        {% elif state.running %}
          <div class="xwait"><span class="pulse"></span> Drafting your reply&hellip;</div>
        {% else %}
          <div class="xnone">No reply drafted yet &mdash; press <b>Draft comments</b> above.</div>
        {% endif %}

        <div class="xactions">
          {% if p.url %}
            <a class="btn btn-sm btn-primary" href="{{ p.url }}"
               target="_blank" rel="noopener noreferrer">&#128279; Go to post</a>
          {% endif %}
          {% if p.comment %}
            <button class="btn btn-sm copybtn" type="button"
                    onclick="copyText(this, {{ p.comment | tojson }})">&#128203; Copy reply</button>
            {% if p.author %}
              <button class="btn btn-sm copybtn" type="button"
                      title="Copies the reply with @{{ p.author }} in front"
                      onclick="copyText(this, {{ ('@' ~ p.author ~ ' ' ~ p.comment) | tojson }})">
                &#128203; Copy with @{{ p.author }}
              </button>
            {% endif %}
          {% endif %}

          <form class="inline" method="post" action="/x/commented/{{ p.post_id }}">
            <label class="donecheck {% if p.done %}on{% endif %}">
              <input type="checkbox" {% if p.done %}checked{% endif %}
                     onchange="this.form.submit()">
              {% if p.done %}Commented{% else %}I have commented{% endif %}
            </label>
          </form>

          <form class="inline" method="post" action="/x/redraft/{{ p.post_id }}"
                style="margin-left:auto;">
            <button class="btn btn-sm btn-ghost" type="submit">&#8635; Redraft</button>
          </form>
          <form class="inline" method="post" action="/x/delete/{{ p.post_id }}"
                onsubmit="return confirm('Delete this post?');">
            <button class="btn btn-sm btn-trash" type="submit"
                    title="Delete this post">&#128465; Delete</button>
          </form>
        </div>
      </div>
    {% endfor %}

    <p class="muted" style="font-size:12.5px; margin-top:4px;">
      Stored on the <b>XPosts</b> sheet in results.xlsx. Anything older than
      {{ max_age }} days is hidden automatically.
    </p>

  {% else %}
    <div class="panel empty">
      <div class="big">&#128172;</div>
      <p>
        Nothing to reply to yet.<br>
        Paste a few posts above, then press <b>Draft comments</b>.
      </p>
    </div>
  {% endif %}
  {% if history %}
    <div class="sectionhead" style="margin-top:34px;">
      <h2>Replies you have made</h2>
      <span class="counts">last {{ history|length }}</span>
    </div>

    <div class="panel">
      {% for p in history %}
        <div class="histrow">
          <div class="histmeta">
            {% if p.author %}
              <a class="xauthor" href="https://x.com/{{ p.author }}"
                 target="_blank" rel="noopener noreferrer">@{{ p.author }}</a>
            {% endif %}
            <span class="xage">{{ p.marked }}</span>
            {% if p.url %}
              <a class="histlink" href="{{ p.url }}"
                 target="_blank" rel="noopener noreferrer">post &#8599;</a>
            {% endif %}
            <form class="inline" method="post" action="/x/commented/{{ p.post_id }}"
                  style="margin-left:auto;">
              <button class="btn btn-sm btn-ghost" type="submit"
                      title="Untick - puts it back on the list">undo</button>
            </form>
          </div>
          {% if p.comment %}
            <div class="histreply">{{ p.comment }}</div>
          {% endif %}
          {% if p.text %}
            <div class="histpost">on: {{ p.text[:140] }}{% if p.text|length > 140 %}&hellip;{% endif %}</div>
          {% endif %}
        </div>
      {% endfor %}
    </div>
  {% endif %}
  <script>
    function copyText(btn, text) {
      navigator.clipboard.writeText(text).then(function () {
        var was = btn.textContent;
        btn.textContent = 'Copied';
        btn.classList.add('done');
        setTimeout(function () { btn.textContent = was; btn.classList.remove('done'); }, 1600);
      });
    }
  </script>

</div>
</body>
</html>
"""


if __name__ == "__main__":
    app.run(port=5001, debug=True, use_reloader=False)
