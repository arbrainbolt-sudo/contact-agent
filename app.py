"""The web page. Run this, then open http://localhost:5000 in your browser."""

import os
import threading

from flask import Flask, request, redirect, url_for, render_template_string

import contact_agent

app = Flask(__name__)

state = {"running": False, "target": "", "log": []}


def log(message):
    print(message)
    state["log"].append(message)


def worker(target):
    try:
        contact_agent.run_agent(target, log=log)
    except Exception as e:
        log(f"ERROR: {e}")
    finally:
        state["running"] = False


@app.route("/")
def home():
    rows = contact_agent.read_rows()
    return render_template_string(
        PAGE,
        rows=rows,
        fields=contact_agent.DISPLAY_FIELDS,
        contacted_count=sum(1 for r in rows if r["contacted"] == "yes"),
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


@app.route("/delete/<row_id>", methods=["POST"])
def delete(row_id):
    contact_agent.delete_row(row_id)
    return redirect(url_for("home"))


@app.route("/toggle/<row_id>", methods=["POST"])
def toggle(row_id):
    contact_agent.toggle_contacted(row_id)
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
    body { font-family: system-ui, sans-serif; margin: 40px auto; max-width: 1400px; color:#222; }
    h1 { font-size: 22px; }
    input[type=text] { padding: 10px; width: 420px; font-size: 15px; }
    .banner { background:#fff6d9; border:1px solid #e3c96a; padding:12px; margin:18px 0; }
    .log { background:#111; color:#0f0; font-family: monospace; font-size:13px;
           padding:12px; height:180px; overflow:auto; white-space:pre-wrap; }
    table { border-collapse: collapse; width: 100%; margin-top: 20px; font-size: 14px; }
    th, td { border: 1px solid #ddd; padding: 6px 9px; text-align: left; vertical-align: top; }
    th { background: #f2f2f2; }
    tr.done { background: #eefbee; color:#667; }
    tr.done td a { color:#779; }
    .muted { color:#777; }
    .iconbtn { border:1px solid #ccc; background:#fff; border-radius:5px; cursor:pointer;
               font-size:15px; line-height:1; padding:5px 9px; }
    .iconbtn:hover { background:#f0f0f0; }
    .tick.on  { background:#2e9c4a; border-color:#2e9c4a; color:#fff; }
    .del:hover { background:#fdeaea; border-color:#d98080; }
    td a { color:#1155cc; }
    form.inline { display:inline; margin:0; }
  </style>
</head>
<body>

  <h1>Contact Finder</h1>

  <form method="post" action="/run">
    <input type="text" name="target" placeholder="e.g. VP of Engineering at Acme Corp, Boston"
           {% if state.running %}disabled{% endif %} autofocus>
    <button class="iconbtn" type="submit" {% if state.running %}disabled{% endif %}>
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

  <h2>Saved results
    <span class="muted">({{ rows|length }} rows · {{ contacted_count }} contacted)</span>
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
            <button class="iconbtn del" type="submit" title="Delete this row">&#128465;</button>
          </form>
        </td>

        {% for f in fields %}
          {% set value = row.get(f, '') %}
          <td>
            {% if f == 'linkedin' and value %}
              <a href="{{ value }}" target="_blank" rel="noopener noreferrer">LinkedIn &#8599;</a>
            {% elif f == 'source_url' and value %}
              <a href="{{ value }}" target="_blank" rel="noopener noreferrer">source &#8599;</a>
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
      <button class="iconbtn" type="submit" {% if state.running %}disabled{% endif %}>Clear all results</button>
    </form>
  {% else %}
    <p class="muted">Nothing saved yet. Enter a target above.</p>
  {% endif %}

</body>
</html>
"""


if __name__ == "__main__":
    app.run(port=5000, debug=True, use_reloader=False)