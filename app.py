"""The web app. Run this, then open http://localhost:5000 in your browser."""

import os
import math
import time
import threading
from datetime import datetime, date, timedelta
from urllib.parse import urlparse

from flask import Flask, request, redirect, url_for, render_template_string

import contact_agent
import crm
import news
import notify
import store

app = Flask(__name__)

state = {"running": False, "target": "", "country": "", "log": []}
news_state = {"running": False, "log": [], "last_run": "", "last_run_ts": 0.0}
crm_state = {"running": False, "log": [], "last_run": "", "sheet": ""}

NO_COMPANY = "__blank__"
NEWS_TIME = os.getenv("NEWS_TIME", "07:00")                 # daily auto-run, 24h clock
NEWS_COOLDOWN = int(os.getenv("NEWS_COOLDOWN", "300"))      # seconds between manual refreshes
CRM_SHEET = os.getenv("CRM_SHEET", "Sales")                 # default worksheet for the CRM tab
CRM_STALE_DAYS = int(os.getenv("CRM_STALE_DAYS", "30"))     # "needs reconnecting" threshold
CRM_WIDE_COL = os.getenv("CRM_WIDE_COL", "activity")        # wide, editable column
CRM_ACTIVITY_COL = os.getenv("CRM_ACTIVITY_COL", "")        # blank = use CRM_WIDE_COL
CRM_MAX_COLS = int(os.getenv("CRM_MAX_COLS", "6"))          # hide columns 7 onwards


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


def crm_boxes(headers, rows):
    """Build the two summary panels shown above the CRM table."""
    company_col = find_col(headers, COMPANY_HINTS, os.getenv("CRM_COMPANY_COL", ""))
    name_col = find_col(headers, NAME_HINTS, os.getenv("CRM_NAME_COL", ""),
                        exclude=(company_col,))
    followup_col = find_col(headers, FOLLOWUP_HINTS, os.getenv("CRM_FOLLOWUP_COL", ""))
    due_col = find_col(headers, DUE_HINTS, os.getenv("CRM_DUE_COL", ""))
    last_col = find_col(headers, LASTCONTACT_HINTS, os.getenv("CRM_LASTCONTACT_COL", ""))
    activity_col = find_col(headers, (CRM_WIDE_COL,), CRM_ACTIVITY_COL)

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = today - timedelta(days=CRM_STALE_DAYS)

    def label(row):
        who = (row.get(name_col, "") if name_col else "").strip()
        org = (row.get(company_col, "") if company_col else "").strip()
        return (who or "(no name)"), org

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
    stale = []
    for row in rows:
        last_raw = (row.get(last_col, "") if last_col else "").strip()
        last = parse_any_date(last_raw)
        if last and last >= cutoff:
            continue                       # contacted recently enough
        who, org = label(row)
        if who == "(no name)" and not org:
            continue
        stale.append({
            "who": who, "org": org,
            "last_raw": last_raw,
            "last": last,
            "days": (today - last).days if last else None,
        })

    stale.sort(key=lambda s: (s["last"] is None, s["last"] or today))

    return {
        "followups": followups,
        "stale": stale,
        "stale_days": CRM_STALE_DAYS,
        "cols": {
            "name": name_col, "company": company_col,
            "followup": followup_col, "due": due_col, "last": last_col,
            "activity": activity_col,
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
    """Runs the news agent once a day at NEWS_TIME."""
    done_on = None
    while True:
        now = datetime.now()
        if now.strftime("%H:%M") == NEWS_TIME and done_on != date.today():
            done_on = date.today()
            print(f"[scheduler] daily news run at {NEWS_TIME}")
            start_news_run(push=True, force=True)
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

    headers, all_rows = store.read_sheet_auto(sheet)
    total = len(all_rows)

    boxes = crm_boxes(headers, all_rows)
    suggestions = crm.merge(all_rows, boxes["cols"], sheet)

    all_cols = [(i + 1, h) for i, h in enumerate(headers)]
    columns = all_cols[:CRM_MAX_COLS]
    wide = (boxes["cols"]["activity"] or CRM_WIDE_COL).strip().lower()
    for col in all_cols[CRM_MAX_COLS:]:
        if wide and wide == col[1].strip().lower():
            columns.append(col)

    rows = all_rows
    if q:
        needle = q.lower()
        rows = [r for r in rows
                if any(needle in (v or "").lower()
                       for k, v in r.items() if k != "_row")]

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
        workbook=store.XLSX_FILE,
        has_logo=logo_exists(),
        notify_status=notify.status_line(),
        tab="crm",
        state=crm_state,
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
    /* ============================================================
       BRAND PALETTE — change these five lines to re-theme the app
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
    tbody td { padding: 9px 12px; vertical-align: top; border-bottom: 1px solid var(--line-soft); height: 1px; }
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

    /* the wide column (Activity by default) — roughly 4x a normal cell */
    th.wide4 { min-width: 620px; }
    td.wide4 {
      min-width: 620px; max-width: 760px;
      white-space: normal; word-break: break-word; line-height: 1.55;
      vertical-align: top;
    }
    td.wide4 textarea.cellin { overflow: hidden; min-height: 0; }

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
    textarea.cellin { resize: vertical; font-size: 12.5px; line-height:1.45; display: block; }    td.editable { min-width: 155px; }
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
    .boxitem .when { font-size: 11.5px; color: var(--ink-faint); margin-top: 4px; }

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
        <span class="sub">Workbench</span>
      </a>

      <nav class="nav">
        <a href="/" class="{% if tab == 'contacts' %}on{% endif %}">Contacts</a>
        <a href="/news" class="{% if tab == 'news' %}on{% endif %}">AI News</a>
        <a href="/crm" class="{% if tab == 'crm' %}on{% endif %}">CRM</a>
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
  <title>Hire2o — Contacts</title>
  {% if state.running %}<meta http-equiv="refresh" content="3">{% endif %}
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
            {% if state.running %}Searching…{% else %}Find contacts{% endif %}
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
      <span>Searching for <b>{{ state.target }}</b>{% if state.country %} in <b>{{ countries[state.country][0] }}</b>{% endif %} — this page refreshes every 3 seconds.</span>
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
          <input type="text" id="q" name="q" value="{{ q }}" placeholder="name, note, email, date…">
        </div>
        <div style="align-self:flex-end; display:flex; gap:8px; align-items:center;">
          <button class="btn" type="submit">Search</button>
          {% if company or q %}<a href="/" style="font-size:13px; color:var(--ink-soft);">Clear filters</a>{% endif %}
        </div>
      </div>
    </form>
  </div>

  <div class="sectionhead">
    <h2>Contacts</h2>
    <span class="counts">showing {{ rows|length }} of {{ total }} · newest first</span>
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
                  <textarea class="cellin" name="value" rows="2" placeholder="Add a note…"
                            onblur="if(this.defaultValue!==this.value){this.form.submit();}">{{ value }}</textarea>
                </form>
              </td>
            {% elif f in editable %}
              <td class="editable">
                <form class="cellform" method="post" action="/edit/{{ row.row_id }}">
                  <input type="hidden" name="field" value="{{ f }}">
                  <input type="hidden" name="company" value="{{ company }}">
                  <input type="hidden" name="q" value="{{ q }}">
                  <input class="cellin" type="text" name="value" value="{{ value }}" placeholder="—"
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
  <title>Hire2o — AI News</title>
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
            {% if state.running %}Fetching…
            {% elif cooldown_left %}Wait {{ (cooldown_left // 60) }}:{{ '%02d' % (cooldown_left % 60) }}
            {% else %}Refresh news{% endif %}
          </button>
          {% if notify_status != 'off' %}
            <button class="btn btn-ghost" type="submit" formaction="/news/test-notify">Test alert</button>
          {% endif %}
        </div>
        <div class="counts">
          Auto-runs daily at <b>{{ news_time }}</b> · {{ total_items }} stories in {{ groups|length }} sections
          {% if state.last_run %} · last run {{ state.last_run }}{% endif %}
        </div>
      </div>
    </form>
  </div>

  {% if state.running %}
    <div class="banner">
      <span class="pulse"></span>
      <span>Fetching, summarising and sorting the latest AI news — this page refreshes every 4 seconds.</span>
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
                      <button class="btn btn-sm btn-star" type="submit">&#9733; Saved — remove</button>
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
  <title>Hire2o — CRM</title>
  {% if state.running %}<meta http-equiv="refresh" content="4">{% endif %}
""" + STYLE + """
</head>
<body>
""" + HEADER + """
<div class="shell">

  <div class="boxrow">

    <div class="actionbox box-calls">
      <div class="boxhead">
        <span class="icon">&#128222;</span>
        <h2>Suggested next actions</h2>
        <span class="n">{{ suggestions|length }}</span>
      </div>

      {% if suggestions %}
        <div class="boxscroll">
          {% for s in suggestions %}
            <div class="sugg p-{{ s.priority }}">
              <div>
                <span class="who">{{ s.who }}</span>
                {% if s.org %}<span class="org">&middot; {{ s.org }}</span>{% endif %}
                <span class="pill pill-{{ s.priority }}">{{ s.priority }}</span>
                {% if s.stale %}<span class="pill pill-stale">notes changed</span>{% endif %}
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
          <span style="font-size:12px;">Press <b>Analyse activity</b> to read the notes and suggest next steps.</span>
        </div>
      {% endif %}

      <div class="boxnote">
        <form method="post" action="/crm/analyze" style="display:flex; align-items:center; gap:10px;">
          <input type="hidden" name="sheet" value="{{ sheet }}">
          <button class="btn btn-sm btn-primary" type="submit" {% if state.running %}disabled{% endif %}>
            {% if state.running %}Analysing…{% else %}&#10024; Analyse activity{% endif %}
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
        <h2>Reconnect — quiet {{ boxes.stale_days }}+ days</h2>
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
                {% if s.last_raw %}Last contact {{ s.last_raw }}{% else %}No contact date recorded{% endif %}
              </div>
            </div>
          {% endfor %}
        </div>
      {% else %}
        <div class="boxempty">
          Everyone contacted within {{ boxes.stale_days }} days.<br>
          {% if not boxes.cols.last %}
            <span style="font-size:12px;">No last-contact column found in this sheet.</span>
          {% endif %}
        </div>
      {% endif %}
      <div class="boxnote">
        Names from <b>{{ boxes.cols.name or '—' }}</b>
        {% if boxes.cols.last %} · dates from <b>{{ boxes.cols.last }}</b>{% endif %}
      </div>
    </div>

  </div>

  {% if state.running %}
    <div class="banner">
      <span class="pulse"></span>
      <span>Reading activity notes and working out next steps — this page refreshes every 4 seconds.</span>
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
          <input type="text" id="q" name="q" value="{{ q }}" placeholder="name, company, stage…">
        </div>
        <div style="align-self:flex-end; display:flex; gap:8px; align-items:center;">
          <button class="btn" type="submit">Search</button>
          {% if q %}<a href="/crm?sheet={{ sheet }}" style="font-size:13px; color:var(--ink-soft);">Clear</a>{% endif %}
          <a class="btn btn-primary" href="/crm?sheet={{ sheet }}&add=1">+ Add row</a>
        </div>
      </div>
    </form>
  </div>

  <div class="sectionhead">
    <h2>{{ sheet }}</h2>
    <span class="counts">showing {{ rows|length }} of {{ total }} rows</span>
    {% if hidden_count %}
      <span class="hiddennote">{{ hidden_count }} column{% if hidden_count != 1 %}s{% endif %} hidden</span>
    {% endif %}
  </div>

  {% if columns %}
    <div class="tablewrap">
      <table>
        <thead>
          <tr>
            {% for col_num, h in columns %}
              <th class="{% if wide_col and wide_col == h.lower() %}wide4{% endif %}">{{ h }}</th>
            {% endfor %}
          </tr>
        </thead>
        <tbody>

          {% if adding %}
            <tr class="addrow">
              <form method="post" action="/crm/add" id="addform">
                <input type="hidden" name="sheet" value="{{ sheet }}">
              </form>
              {% for col_num, h in columns %}
                <td class="{% if wide_col and wide_col == h.lower() %}wide4{% endif %}">
                  {% if wide_col and wide_col == h.lower() %}
                    <textarea form="addform" name="col_{{ col_num }}" rows="3" placeholder="{{ h }}…"></textarea>
                  {% else %}
                    <input form="addform" type="text" name="col_{{ col_num }}" placeholder="{{ h }}">
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
              {% for col_num, h in columns %}
                {% set value = row.get(h, '') %}
                {% if wide_col and wide_col == h.lower() %}
                  <td class="wide4">
                    <form class="cellform" method="post" action="/crm/edit">
                      <input type="hidden" name="sheet" value="{{ sheet }}">
                      <input type="hidden" name="row" value="{{ row._row }}">
                      <input type="hidden" name="col" value="{{ col_num }}">
                      <input type="hidden" name="q" value="{{ q }}">
                      <textarea class="cellin autogrow" name="value"
                                rows="{{ value | rows_for }}"
                                placeholder="Add activity…"
                                oninput="autogrow(this)"
                                onblur="if(this.defaultValue!==this.value){this.form.submit();}">{{ value }}</textarea>
                    </form>
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

    <p class="muted" style="font-size:12.5px; margin-top:14px;">
      <b>{{ workbook }}</b> → sheet <b>{{ sheet }}</b>.
      The <b>{{ wide_col }}</b> column saves when you click away from it.
      Keep the file closed in Excel while using this page.
    </p>

  {% else %}
    <div class="panel empty">
      <div class="big">&#128202;</div>
      <p>
        No worksheet named <b>{{ sheet }}</b> found in {{ workbook }} — or it has no header row.<br>
        Add column names in row 1, save and close Excel, then reload.
      </p>
    </div>
  {% endif %}

  <script>
    function autogrow(el) {
      el.style.height = 'auto';
      el.style.height = (el.scrollHeight + 2) + 'px';
    }
    document.addEventListener('DOMContentLoaded', function () {
      document.querySelectorAll('textarea.autogrow').forEach(autogrow);
    });
  </script>

</div>
</body>
</html>
"""


if __name__ == "__main__":
    app.run(port=5000, debug=True, use_reloader=False)