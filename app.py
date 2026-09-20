"""The web app. Run this, then open http://localhost:5000 in your browser."""

import os
import time
import threading
from datetime import datetime, date
from urllib.parse import urlparse

from flask import Flask, request, redirect, url_for, render_template_string

import contact_agent
import news
import notify

app = Flask(__name__)

state = {"running": False, "target": "", "country": "", "log": []}
news_state = {"running": False, "log": [], "last_run": ""}

NO_COMPANY = "__blank__"
NEWS_TIME = os.getenv("NEWS_TIME", "07:00")     # daily auto-run, 24h clock


# ---------------------------------------------------------------- helpers

def domain_of(url):
    try:
        host = urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else (host or url)
    except Exception:
        return url


app.jinja_env.filters["domain"] = domain_of


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


# ---------------------------------------------------------------- workers

def log(message):
    print(message)
    state["log"].append(message)


def news_log(message):
    print(message)
    news_state["log"].append(message)


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


def start_news_run(push=True):
    if news_state["running"]:
        return False
    news_state["running"] = True
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
            start_news_run(push=True)
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
    return render_template_string(
        NEWS_PAGE,
        items=news.latest(),
        saved_items=news.saved(),
        saved_ids=news.saved_ids(),
        has_logo=logo_exists(),
        notify_status=notify.status_line(),
        news_time=NEWS_TIME,
        tab="news",
        state=news_state,
    )


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
  <style>
    body { font-family: system-ui, sans-serif; margin: 30px auto; max-width: 1600px; color:#222; }
    .topbar { display:flex; align-items:center; gap:14px; margin-bottom:6px; }
    .topbar img { height:42px; width:auto; }
    .wordmark { font-size:24px; font-weight:700; letter-spacing:-0.5px; color:#1a3a6b; }
    .notifybar { font-size:12.5px; color:#777; margin:0 0 14px 2px; }
    .tabs { border-bottom:2px solid #dfe2e6; margin-bottom:20px; }
    .tabs a { display:inline-block; padding:9px 20px; text-decoration:none; color:#555;
              font-size:15px; border:2px solid transparent; border-bottom:none; }
    .tabs a.on { color:#1a3a6b; font-weight:600; background:#fff;
                 border-color:#dfe2e6; border-radius:6px 6px 0 0; margin-bottom:-2px; }
    input[type=text] { padding: 9px; font-size: 15px; }
    #target { width: 420px; }
    #q { width: 300px; }
    select { padding: 9px; font-size: 15px; max-width: 320px; }
    .filters { background:#f6f7f9; border:1px solid #dfe2e6; padding:14px; margin:22px 0 6px; }
    .filters label { font-size:13px; color:#555; margin-right:6px; }
    .banner { background:#fff6d9; border:1px solid #e3c96a; padding:12px; margin:14px 0; }
    .log { background:#111; color:#0f0; font-family: monospace; font-size:13px;
           padding:12px; height:150px; overflow:auto; white-space:pre-wrap; margin:12px 0; }
    table { border-collapse: collapse; width: 100%; margin-top: 14px; font-size: 14px; }
    th, td { border: 1px solid #ddd; padding: 6px 9px; text-align: left; vertical-align: top; }
    th { background: #f2f2f2; }
    tr.done { background: #eefbee; color:#667; }
    tr.done td a { color:#779; }
    .muted { color:#777; font-weight: normal; }
    .iconbtn { border:1px solid #ccc; background:#fff; border-radius:5px; cursor:pointer;
               font-size:15px; line-height:1; padding:6px 10px; }
    .iconbtn:hover { background:#f0f0f0; }
    .small { font-size:12px; padding:4px 8px; }
    .tick.on  { background:#2e9c4a; border-color:#2e9c4a; color:#fff; }
    .del:hover { background:#fdeaea; border-color:#d98080; }
    td a { color:#1155cc; }
    form.inline { display:inline; margin:0; }
    .url { word-break: break-all; font-size:12.5px; max-width:300px; display:inline-block; }
    .srcname { font-weight:600; }
    .cellform { margin:0; display:flex; gap:4px; align-items:flex-start; }
    .cellin { border:1px solid transparent; background:transparent; font:inherit;
              color:inherit; padding:3px 5px; border-radius:4px; width:100%; }
    .cellin:hover { border-color:#ddd; background:#fff; }
    .cellin:focus { border-color:#7aa7e6; background:#fff; outline:none;
                    box-shadow:0 0 0 2px #e4edfb; }
    textarea.cellin { resize:vertical; min-height:32px; font-size:13px; }
    td.editable { min-width:150px; }
    td.commentcell { min-width:210px; max-width:260px; }

    .newswrap { display:grid; grid-template-columns: 1fr 380px; gap:26px; align-items:start; }
    .card { border:1px solid #e2e5e9; border-radius:8px; padding:15px 17px; margin-bottom:14px;
            background:#fff; }
    .card h3 { margin:0 0 7px; font-size:16.5px; line-height:1.35; }
    .card h3 a { color:#12264a; text-decoration:none; }
    .card h3 a:hover { text-decoration:underline; }
    .card p { margin:0 0 10px; font-size:14px; line-height:1.55; color:#333; }
    .meta { font-size:12px; color:#888; margin-bottom:8px; }
    .cardlink { font-size:12.5px; word-break:break-all; }
    .savedcol { border:1px solid #dfe2e6; border-radius:8px; background:#f8f9fb;
                padding:14px; position:sticky; top:20px; }
    .savedcol h2 { margin:0 0 10px; font-size:16px; }
    .savedscroll { max-height:70vh; overflow-y:auto; padding-right:6px; }
    .savedcard { border-bottom:1px solid #e4e7ea; padding:10px 0; }
    .savedcard:last-child { border-bottom:none; }
    .savedcard a { font-size:14px; color:#12264a; font-weight:600; text-decoration:none; }
    .savedcard a:hover { text-decoration:underline; }
    .savedcard .meta { margin:4px 0 6px; }
  </style>
"""

HEADER = """
  <div class="topbar">
    {% if has_logo %}
      <img src="{{ url_for('static', filename='logo.png') }}" alt="Hire2o">
    {% else %}
      <span class="wordmark">Hire2o</span>
    {% endif %}
    <span class="wordmark" style="font-weight:400; color:#555;">Workbench</span>
  </div>
  <p class="notifybar">Notifications: {{ notify_status }}</p>

  <div class="tabs">
    <a href="/" class="{% if tab == 'contacts' %}on{% endif %}">Contacts</a>
    <a href="/news" class="{% if tab == 'news' %}on{% endif %}">AI News</a>
  </div>
"""

CONTACTS_PAGE = """
<!doctype html>
<html>
<head>
  <title>Hire2o — Contacts</title>
  {% if state.running %}<meta http-equiv="refresh" content="3">{% endif %}
""" + STYLE + """
</head>
<body>
""" + HEADER + """

  <form method="post" action="/run">
    <input type="hidden" name="company" value="{{ company }}">
    <input type="hidden" name="q" value="{{ q }}">
    <input type="text" id="target" name="target"
           placeholder="e.g. VP of Engineering at Acme Corp"
           {% if state.running %}disabled{% endif %} autofocus>

    <select name="country_code" {% if state.running %}disabled{% endif %}>
      {% for code, info in countries.items() %}
        <option value="{{ code }}" {% if code == state.country %}selected{% endif %}>{{ info[0] }}</option>
      {% endfor %}
    </select>

    <button class="iconbtn" type="submit" {% if state.running %}disabled{% endif %}>
      {% if state.running %}Running...{% else %}Find contacts{% endif %}
    </button>

    <button class="iconbtn small" type="submit" formaction="/test-notify">send test</button>
  </form>

  {% if state.running %}
    <div class="banner">
      Working on <b>{{ state.target }}</b>
      {% if state.country %}in <b>{{ countries[state.country][0] }}</b>{% endif %}
      — refreshing every 3 seconds.
    </div>
  {% endif %}

  {% if state.log %}
    <div class="log">{% for line in state.log %}{{ line }}
{% endfor %}</div>
  {% endif %}

  <div class="filters">
    <form method="get" action="/">
      <label for="company">Company</label>
      <select id="company" name="company" onchange="this.form.submit()">
        <option value="">All companies ({{ total }})</option>
        {% for c in companies %}
          <option value="{{ c }}" {% if c == company %}selected{% endif %}>{{ c }}</option>
        {% endfor %}
        <option value="{{ no_company }}" {% if company == no_company %}selected{% endif %}>
          (no company listed)
        </option>
      </select>

      <label for="q" style="margin-left:18px;">Search</label>
      <input type="text" id="q" name="q" value="{{ q }}" placeholder="any text in any column...">
      <button class="iconbtn" type="submit">Search</button>
      {% if company or q %}<a href="/" style="margin-left:14px;">clear filters</a>{% endif %}
    </form>
  </div>

  <h2>Results
    <span class="muted">showing {{ rows|length }} of {{ total }} · {{ contacted_count }} contacted</span>
  </h2>

  {% if rows %}
    <table>
      <tr>
        <th>Done</th><th>Del</th>
        {% for f in fields %}<th>{{ f }}</th>{% endfor %}
      </tr>
      {% for row in rows %}
      <tr class="{% if row.contacted == 'yes' %}done{% endif %}">
        <td>
          <form class="inline" method="post" action="/toggle/{{ row.row_id }}">
            <input type="hidden" name="company" value="{{ company }}">
            <input type="hidden" name="q" value="{{ q }}">
            <button class="iconbtn tick {% if row.contacted == 'yes' %}on{% endif %}" type="submit">&#10003;</button>
          </form>
        </td>
        <td>
          <form class="inline" method="post" action="/delete/{{ row.row_id }}"
                onsubmit="return confirm('Delete {{ row.name|e }}?');">
            <input type="hidden" name="company" value="{{ company }}">
            <input type="hidden" name="q" value="{{ q }}">
            <button class="iconbtn del" type="submit">&#128465;</button>
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
                <textarea class="cellin" name="value" rows="2" placeholder="add a note..."
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
                <a href="mailto:{{ value }}" style="font-size:12px;">write &#8599;</a>
              {% endif %}
            </td>
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
    </table>

    <form method="post" action="/clear" style="margin-top:16px;">
      <button class="iconbtn" type="submit" {% if state.running %}disabled{% endif %}>Clear all results</button>
    </form>
  {% elif total %}
    <p class="muted">No rows match this filter. <a href="/">Clear filters</a> to see all {{ total }}.</p>
  {% else %}
    <p class="muted">Nothing saved yet. Enter a target above.</p>
  {% endif %}

</body>
</html>
"""

NEWS_PAGE = """
<!doctype html>
<html>
<head>
  <title>Hire2o — AI News</title>
  {% if state.running %}<meta http-equiv="refresh" content="4">{% endif %}
""" + STYLE + """
</head>
<body>
""" + HEADER + """

  <form method="post" action="/news/run" style="margin-bottom:6px;">
    <button class="iconbtn" type="submit" {% if state.running %}disabled{% endif %}>
      {% if state.running %}Fetching...{% else %}Refresh news{% endif %}
    </button>
    {% if notify_status != 'off' %}
      <button class="iconbtn small" type="submit" formaction="/news/test-notify">send test</button>
    {% endif %}
    <span class="muted" style="margin-left:12px; font-size:13px;">
      Runs automatically at {{ news_time }} daily · {{ items|length }} stories
      {% if state.last_run %}· last run {{ state.last_run }}{% endif %}
    </span>
  </form>

  {% if state.running %}
    <div class="banner">Fetching and summarising — this page refreshes every 4 seconds.</div>
  {% endif %}

  {% if state.log %}
    <div class="log">{% for line in state.log %}{{ line }}
{% endfor %}</div>
  {% endif %}

  <div class="newswrap">

    <div>
      {% if items %}
        {% for item in items %}
          <div class="card">
            <h3><a href="{{ item.url }}" target="_blank" rel="noopener noreferrer">{{ item.title }}</a></h3>
            <div class="meta">
              {% if item.source %}{{ item.source }}{% else %}{{ item.url | domain }}{% endif %}
              {% if item.published %} · {{ item.published }}{% endif %}
            </div>
            <p>{{ item.summary }}</p>
            <a class="cardlink" href="{{ item.url }}" target="_blank" rel="noopener noreferrer">{{ item.url }}</a>
            <div style="margin-top:11px;">
              {% if item.news_id in saved_ids %}
                <form class="inline" method="post" action="/news/unsave/{{ item.news_id }}">
                  <button class="iconbtn small" type="submit">&#9733; saved — remove</button>
                </form>
              {% else %}
                <form class="inline" method="post" action="/news/save/{{ item.news_id }}">
                  <button class="iconbtn small" type="submit">&#9734; save for later</button>
                </form>
              {% endif %}
              {% if notify_status != 'off' %}
                <form class="inline" method="post" action="/news/push/{{ item.news_id }}">
                  <button class="iconbtn small" type="submit" title="Send this story to your phone">
                    &#128241; send to phone
                  </button>
                </form>
              {% endif %}
            </div>
          </div>
        {% endfor %}
      {% else %}
        <p class="muted">No news yet. Press <b>Refresh news</b> to fetch today's stories.</p>
      {% endif %}
    </div>

    <div class="savedcol">
      <h2>Saved for later <span class="muted">({{ saved_items|length }})</span></h2>
      {% if saved_items and notify_status != 'off' %}
        <form method="post" action="/news/push-saved" style="margin-bottom:10px;">
          <button class="iconbtn small" type="submit">&#128241; send all saved to phone</button>
        </form>
      {% endif %}
      <div class="savedscroll">
        {% if saved_items %}
          {% for item in saved_items %}
            <div class="savedcard">
              <a href="{{ item.url }}" target="_blank" rel="noopener noreferrer">{{ item.title }}</a>
              <div class="meta">
                {% if item.source %}{{ item.source }} · {% endif %}saved {{ item.saved_at }}
              </div>
              <form class="inline" method="post" action="/news/unsave/{{ item.news_id }}">
                <button class="iconbtn small" type="submit">remove</button>
              </form>
              {% if notify_status != 'off' %}
                <form class="inline" method="post" action="/news/push/{{ item.news_id }}">
                  <button class="iconbtn small" type="submit">&#128241;</button>
                </form>
              {% endif %}
            </div>
          {% endfor %}
        {% else %}
          <p class="muted" style="font-size:13px;">Nothing saved yet. Use the star button on any story.</p>
        {% endif %}
      </div>
    </div>

  </div>

</body>
</html>
"""


if __name__ == "__main__":
    app.run(port=5000, debug=True, use_reloader=False)