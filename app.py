"""The web page. Run this, then open http://localhost:5000 in your browser."""

import os
import threading

from flask import Flask, request, redirect, url_for, render_template_string

import contact_agent

app = Flask(__name__)

# Shared notepad the background worker writes to and the page reads from.
state = {"running": False, "target": "", "log": []}


def log(message):
    print(message)                 # shows in the terminal
    state["log"].append(message)   # shows on the web page


def worker(target):
    """Runs the agent on a background thread so the browser isn't frozen."""
    try:
        contact_agent.run_agent(target, log=log)
    except Exception as e:
        log(f"ERROR: {e}")
    finally:
        state["running"] = False


@app.route("/")
def home():
    return render_template_string(
        PAGE,
        rows=contact_agent.read_rows(),
        fields=contact_agent.FIELDS,
        state=state,
    )


@app.route("/run", methods=["POST"])
def run():
    target = request.form.get("target", "").strip()
    if target and not state["running"]:
        state["running"] = True
        state["target"] = target
        state["log"] = []
        threading.Thread(target=worker, args=(target,), daemon=True).start()
    return redirect(url_for("home"))


@app.route("/clear", methods=["POST"])
def clear():
    if not state["running"] and os.path.exists(contact_agent.CSV_FILE):
        os.remove(contact_agent.CSV_FILE)
    return redirect(url_for("home"))


PAGE = """
<!doctype html>
<html>
<head>
  <title>Contact Finder</title>
  {% if state.running %}<meta http-equiv="refresh" content="3">{% endif %}
  <style>
    body { font-family: system-ui, sans-serif; margin: 40px auto; max-width: 1100px; color:#222; }
    h1 { font-size: 22px; }
    input[type=text] { padding: 10px; width: 420px; font-size: 15px; }
    button { padding: 10px 18px; font-size: 15px; cursor: pointer; }
    .banner { background:#fff6d9; border:1px solid #e3c96a; padding:12px; margin:18px 0; }
    .log { background:#111; color:#0f0; font-family: monospace; font-size:13px;
           padding:12px; height:180px; overflow:auto; white-space:pre-wrap; }
    table { border-collapse: collapse; width: 100%; margin-top: 20px; font-size: 14px; }
    th, td { border: 1px solid #ddd; padding: 7px 9px; text-align: left; vertical-align: top; }
    th { background: #f2f2f2; }
    tr:nth-child(even) { background: #fafafa; }
    .muted { color:#777; }
  </style>
</head>
<body>

  <h1>Contact Finder</h1>

  <form method="post" action="/run">
    <input type="text" name="target" placeholder="e.g. Head of Talent at Acme Corp"
           {% if state.running %}disabled{% endif %} autofocus>
    <button type="submit" {% if state.running %}disabled{% endif %}>
      {% if state.running %}Running...{% else %}Find contacts{% endif %}
    </button>
  </form>

  {% if state.running %}
    <div class="banner">Working on <b>{{ state.target }}</b> — this page refreshes every 3 seconds.</div>
  {% endif %}

  {% if state.log %}
    <div class="log">{% for line in state.log %}{{ line }}
{% endfor %}</div>
  {% endif %}

  <h2>Saved results <span class="muted">({{ rows|length }} rows in results.csv)</span></h2>

  {% if rows %}
    <table>
      <tr>{% for f in fields %}<th>{{ f }}</th>{% endfor %}</tr>
      {% for row in rows %}
        <tr>{% for f in fields %}<td>{{ row.get(f, "") }}</td>{% endfor %}</tr>
      {% endfor %}
    </table>
    <form method="post" action="/clear" style="margin-top:16px;">
      <button type="submit" {% if state.running %}disabled{% endif %}>Clear all results</button>
    </form>
  {% else %}
    <p class="muted">Nothing saved yet. Enter a target above.</p>
  {% endif %}

</body>
</html>
"""


if __name__ == "__main__":
    app.run(port=5000, debug=True, use_reloader=False)