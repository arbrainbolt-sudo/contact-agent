"""The web page. Run this, then open http://localhost:5000 in your browser."""

import os
import threading
from urllib.parse import urlparse

from flask import Flask, request, redirect, url_for, render_template_string

import contact_agent

app = Flask(__name__)

state = {"running": False, "target": "", "country": "", "log": []}

NO_COMPANY = "__blank__"     # dropdown value for rows with no company


# ---------------------------------------------------------------- helpers

def domain_of(url):
    """'https://www.acme.com/team/bios?x=1'  ->  'acme.com'"""
    try:
        host = urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else (host or url)
    except Exception:
        return url


app.jinja_env.filters["domain"] = domain_of


def company_list(rows):
    """Every distinct company in the data, sorted, for the dropdown."""
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
    """Return to the home page with the current filters still applied."""
    params = {k: v for k, v in (("company", source.get("company", "")),
                                ("q", source.get("q", ""))) if v}
    return redirect(url_for("home", **params))


def logo_exists():
    return os.path.exists(os.path.join(app.static_folder, "logo.png"))


# ---------------------------------------------------------------- the agent

def log(message):
    print(message)
    state["log"].append(message)


def worker(target, country_code):
    try:
        contact_agent.run_agent(target, country_code=country_code, log=log)
    except Exception as e:
        log(f"ERROR: {e}")
    finally:
        state["running"] = False


# ---------------------------------------------------------------- routes

@app.route("/")
def home():
    all_rows = contact_agent.read_rows()

    company = request.args.get("company", "")
    q = request.args.get("q", "")
    visible = apply_filters(all_rows, company, q)

    return render_template_string(
        PAGE,
        rows=visible,
        total=len(all_rows),
        companies=company_list(all_rows),
        countries=contact_agent.COUNTRIES,
        company=company,
        q=q,
        no_company=NO_COMPANY,
        contacted_count=sum(1 for r in visible if r["contacted"] == "yes"),
        fields=contact_agent.DISPLAY_FIELDS,
        has_logo=logo_exists(),
        state=state,
    )


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
    if not state["running"] and os.path.exists(contact_agent.CSV_FILE):
        os.remove(contact_agent.CSV_FILE)
    return redirect(url_for("home"))


# ---------------------------------------------------------------- the page

PAGE = """
<!doctype html>
<html>
<head>
  <title>Hire2o Contact Finder</title>
  {% if state.running %}<meta http-equiv="refresh" content="3">{% endif %}
  <style>
    body { font-family: system-ui, sans-serif; margin: 40px auto; max-width: 1500px; color:#222; }
    .topbar { display:flex; align-items:center; gap:14px; margin-bottom:18px; }
    .topbar img { height:42px; width:auto; }
    .wordmark { font-size:24px; font-weight:700; letter-spacing:-0.5px; color:#1a3a6b; }
    input[type=text] { padding: 9px; font-size: 15px; }
    #target { width: 420px; }
    #q { width: 300px; }
    select { padding: 9px; font-size: 15px; max-width: 320px; }
    .filters { background:#f6f7f9; border:1px solid #dfe2e6; padding:14px; margin:22px 0 6px; }
    .filters label { font-size:13px; color:#555; margin-right:6px; }
    .banner { background:#fff6d9; border:1px solid #e3c96a; padding:12px; margin:18px 0; }
    .log { background:#111; color:#0f0; font-family: monospace; font-size:13px;
           padding:12px; height:180px; overflow:auto; white-space:pre-wrap; }
    table { border-collapse: collapse; width: 100%; margin-top: 14px; font-size: 14px; }
    th, td { border: 1px solid #ddd; padding: 6px 9px; text-align: left; vertical-align: top; }
    th { background: #f2f2f2; }
    tr.done { background: #eefbee; color:#667; }
    tr.done td a { color:#779; }
    .muted { color:#777; font-weight: normal; }
    .iconbtn { border:1px solid #ccc; background:#fff; border-radius:5px; cursor:pointer;
               font-size:15px; line-height:1; padding:6px 10px; }
    .iconbtn:hover { background:#f0f0f0; }
    .tick.on  { background:#2e9c4a; border-color:#2e9c4a; color:#fff; }
    .del:hover { background:#fdeaea; border-color:#d98080; }
    td a { color:#1155cc; }
    form.inline { display:inline; margin:0; }
    .url { word-break: break-all; font-size:12.5px; max-width:300px; display:inline-block; }
    .srcname { font-weight:600; }
  </style>
</head>
<body>

  <div class="topbar">
    {% if has_logo %}
      <img src="{{ url_for('static', filename='logo.png') }}" alt="Hire2o">
    {% else %}
      <span class="wordmark">Hire2o</span>
    {% endif %}
    <span class="wordmark" style="font-weight:400; color:#555;">Contact Finder</span>
  </div>

  <form method="post" action="/run">
    <input type="hidden" name="company" value="{{ company }}">
    <input type="hidden" name="q" value="{{ q }}">
    <input type="text" id="target" name="target"
           placeholder="e.g. VP of Engineering at Acme Corp"
           {% if state.running %}disabled{% endif %} autofocus>

    <select name="country_code" id="country_code" {% if state.running %}disabled{% endif %}>
      {% for code, info in countries.items() %}
        <option value="{{ code }}" {% if code == state.country %}selected{% endif %}>
          {{ info[0] }}
        </option>
      {% endfor %}
    </select>

    <button class="iconbtn" type="submit" {% if state.running %}disabled{% endif %}>
      {% if state.running %}Running...{% else %}Find contacts{% endif %}
    </button>
  </form>

  {% if state.running %}
    <div class="banner">
      Working on <b>{{ state.target }}</b>
      {% if state.country %}in <b>{{ countries[state.country][0] }}</b>{% endif %}
      — this page refreshes every 3 seconds.
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
      <input type="text" id="q" name="q" value="{{ q }}"
             placeholder="any text in any column...">
      <button class="iconbtn" type="submit">Search</button>

      {% if company or q %}
        <a href="/" style="margin-left:14px;">clear filters</a>
      {% endif %}
    </form>
  </div>

  <h2>
    Results
    <span class="muted">
      showing {{ rows|length }} of {{ total }} · {{ contacted_count }} contacted
    </span>
  </h2>

  {% if rows %}
    <table>
      <tr>
        <th>Done</th>
        <th>Del</th>
        {% for f in fields %}<th>{{ f }}</th>{% endfor %}
      </tr>

      {% for row in rows %}
      <tr class="{% if row.contacted == 'yes' %}done{% endif %}">

        <td>
          <form class="inline" method="post" action="/toggle/{{ row.row_id }}">
            <input type="hidden" name="company" value="{{ company }}">
            <input type="hidden" name="q" value="{{ q }}">
            <button class="iconbtn tick {% if row.contacted == 'yes' %}on{% endif %}"
                    type="submit"
                    title="{% if row.contacted == 'yes' %}Mark as not contacted{% else %}Mark as contacted{% endif %}">
              &#10003;
            </button>
          </form>
        </td>

        <td>
          <form class="inline" method="post" action="/delete/{{ row.row_id }}"
                onsubmit="return confirm('Delete {{ row.name|e }}?');">
            <input type="hidden" name="company" value="{{ company }}">
            <input type="hidden" name="q" value="{{ q }}">
            <button class="iconbtn del" type="submit" title="Delete this row">&#128465;</button>
          </form>
        </td>

        {% for f in fields %}
          {% set value = row.get(f, '') %}
          <td>
            {% if f == 'linkedin' and value %}
              <a class="url" href="{{ value }}" target="_blank" rel="noopener noreferrer">{{ value }}</a>
            {% elif f == 'source_url' and value %}
              <a href="{{ value }}" target="_blank" rel="noopener noreferrer" title="{{ value }}">
                <span class="srcname">{{ value | domain }}</span> &#8599;
              </a>
              <div class="url muted">{{ value }}</div>
            {% elif f == 'email' and value %}
              <a href="mailto:{{ value }}">{{ value }}</a>
            {% else %}
              {{ value }}
            {% endif %}
          </td>
        {% endfor %}

      </tr>
      {% endfor %}
    </table>

    <form method="post" action="/clear" style="margin-top:16px;">
      <button class="iconbtn" type="submit" {% if state.running %}disabled{% endif %}>
        Clear all results
      </button>
    </form>

  {% elif total %}
    <p class="muted">No rows match this filter. <a href="/">Clear filters</a> to see all {{ total }}.</p>
  {% else %}
    <p class="muted">Nothing saved yet. Enter a target above.</p>
  {% endif %}

</body>
</html>
"""


if __name__ == "__main__":
    app.run(port=5000, debug=True, use_reloader=False)