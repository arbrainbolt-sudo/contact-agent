"""
Open Reqs tab - one card per open requisition, with a running list of the
candidates put forward against it.

Two worksheets in the same workbook as everything else:
    OpenReqs            one row per requisition
    OpenReqCandidates   one row per name/status pair, tied to a Req ID

Wired into app.py with:
    from openreqs import bp as openreqs_bp
    app.register_blueprint(openreqs_bp)
"""

import os
import re
import contextlib
from datetime import datetime

from flask import Blueprint, request, redirect, url_for, render_template_string, jsonify

import store
import notify

bp = Blueprint("openreqs", __name__)


REQ_SHEET = os.getenv("OPENREQ_SHEET", "OpenReqs")
CAND_SHEET = os.getenv("OPENREQ_CAND_SHEET", "OpenReqCandidates")

REQ_HEADERS = ("Req ID", "Priority", "Qty", "Client", "Req", "Work Auth", "Date",
               "Rate", "Location", "Relocation", "Type", "Added")
CAND_HEADERS = ("Req ID", "Name", "Status", "TA", "Struck", "Highlight", "Added")

# the order the fields appear on screen, and what each sort button offers
FIELD_ORDER = ("Priority", "Qty", "Client", "Req", "Work Auth", "Date", "Rate",
               "Location", "Relocation", "Type")

QTY_CHOICES = [str(n) for n in range(1, 100)]
RELOCATION_CHOICES = ("Remote", "Hybrid", "On site")
WORKAUTH_CHOICES = ("OPT+", "H1+", "GC+", "USC Only")
TA_CHOICES = ("", "MJ", "SK", "PK", "PH")       # who is working the candidate
TYPE_CHOICES = ("Contract", "C2H", "FT")

# same four colours as the CRM priority swatch
PRIORITY_COLOURS = ("blue", "red", "yellow", "green")
PRIORITY_LABELS = {"blue": "Blue", "red": "Red", "yellow": "Yellow", "green": "Green"}
PRIORITY_MEANING = {"red": "Hot", "yellow": "Warm", "green": "Room Temp", "blue": "Cold"}
PRIORITY_ORDER = {"red": 0, "yellow": 1, "green": 2, "blue": 3}
PRIORITY_LOOKUP = {"hot": "red", "warm": "yellow", "cold": "blue",
                   "room temp": "green", "roomtemp": "green", "room temperature": "green"}
for _colour in PRIORITY_COLOURS:
    PRIORITY_LOOKUP[_colour] = _colour


def priority_of(text):
    return PRIORITY_LOOKUP.get((text or "").strip().lower(), "blue")


def priority_label(colour):
    return PRIORITY_LABELS.get(colour, "Blue")


def _store_lock():
    """Reuse store's own write lock when it has one, so direct writes stay safe."""
    for name in ("_LOCK", "LOCK", "_lock", "lock"):
        candidate = getattr(store, name, None)
        if hasattr(candidate, "__enter__"):
            return candidate
    return contextlib.nullcontext()


def _header_map(page):
    """Header text -> column number, read from row 1 as the sheet actually has it."""
    found = {}
    for col in range(1, max(page.max_column, 1) + 1):
        name = str(page.cell(row=1, column=col).value or "").strip()
        if name and name not in found:
            found[name] = col
    return found


def _sheet(book, name, headers):
    """Make the sheet if it is missing, and add any column the code knows but it lacks.

    Columns are addressed by their header text, never by position, so a new
    field appends to the end of an existing sheet without disturbing the data.
    """
    if name not in book.sheetnames:
        page = book.create_sheet(name)
        for i, header in enumerate(headers, start=1):
            page.cell(row=1, column=i).value = header
        print(f"created the {name} sheet in {store.XLSX_FILE}")
        return page, True

    page = book[name]
    found = _header_map(page)
    if not found:
        for i, header in enumerate(headers, start=1):
            page.cell(row=1, column=i).value = header
        return page, True

    changed = False
    at = max(found.values())
    for header in headers:
        if header not in found:
            at += 1
            page.cell(row=1, column=at).value = header
            found[header] = at
            changed = True
            print(f"added a {header!r} column to {name} in {store.XLSX_FILE}")
    return page, changed


def open_book():
    """The workbook, with both sheets guaranteed to exist."""
    import openpyxl
    book = openpyxl.load_workbook(store.XLSX_FILE)
    _, made_a = _sheet(book, REQ_SHEET, REQ_HEADERS)
    _, made_b = _sheet(book, CAND_SHEET, CAND_HEADERS)
    if made_a or made_b:
        book.save(store.XLSX_FILE)
    return book


def _rows_of(page, headers):
    columns = _header_map(page)
    out = []
    for r in range(2, page.max_row + 1):
        row = {"_row": r}
        for header in headers:
            col = columns.get(header)
            row[header] = str(page.cell(row=r, column=col).value or "").strip() if col else ""
        if not any(row[h] for h in headers):
            continue
        out.append(row)
    return out


def read_reqs():
    """Every requisition, newest first, each with its candidate list attached."""
    with _store_lock():
        book = open_book()
        reqs = _rows_of(book[REQ_SHEET], REQ_HEADERS)
        cands = _rows_of(book[CAND_SHEET], CAND_HEADERS)
        book.close()

    by_req = {}
    for cand in cands:
        by_req.setdefault(cand["Req ID"], []).append(cand)

    for req in reqs:
        req["colour"] = priority_of(req.get("Priority", ""))
        req["candidates"] = by_req.get(req["Req ID"], [])
        for person in req["candidates"]:
            person["struck"] = str(person.get("Struck", "")).strip().lower() in ("yes", "1", "true")
            person["green"] = str(person.get("Highlight", "")).strip().lower() in ("green", "yes", "1", "true")
            person["ta"] = (person.get("TA") or "").strip()
        try:
            req["_qty"] = int(req.get("Qty") or 0)
        except ValueError:
            req["_qty"] = 0
    return reqs


def filter_reqs(reqs, q="", client="", kind="", prio=""):
    """The one place the page and the chart agree on what is being shown."""
    shown = list(reqs)
    if client:
        shown = [r for r in shown if r["Client"] == client]
    if kind:
        shown = [r for r in shown if r["Type"] == kind]
    if prio:
        shown = [r for r in shown if r["colour"] == prio]
    if q:
        needle = q.lower()

        def hit(req):
            fields = [str(req.get(h, "")) for h in REQ_HEADERS]
            for cand in req["candidates"]:
                fields += [cand.get("Name", ""), cand.get("Status", ""), cand.get("TA", "")]
            return any(needle in f.lower() for f in fields)

        shown = [r for r in shown if hit(r)]
    return shown


def _money(text):
    """'$78/hr' or '$140k' -> a number we can sort on."""
    cleaned = re.sub(r"[^0-9.kK]", "", str(text or ""))
    multiplier = 1000 if cleaned[-1:].lower() == "k" else 1
    cleaned = cleaned.rstrip("kK")
    try:
        return float(cleaned or 0) * multiplier
    except ValueError:
        return 0.0


def sort_reqs(reqs, field, direction):
    """Sort by any one field. Priority runs hot-first, Qty and Rate numerically."""
    backwards = direction == "desc"

    if field == "Priority":
        key = lambda r: PRIORITY_ORDER.get(r["colour"], 3)
    elif field == "Qty":
        key = lambda r: r["_qty"]
    elif field == "Rate":
        key = lambda r: _money(r.get("Rate", ""))
    elif field in FIELD_ORDER:
        key = lambda r: str(r.get(field, "")).strip().lower()
    else:                                   # nothing chosen: hottest, then soonest
        return sorted(reqs, key=lambda r: (PRIORITY_ORDER.get(r["colour"], 3),
                                           r.get("Date", "")))
    return sorted(reqs, key=key, reverse=backwards)


def add_req(values):
    with _store_lock():
        book = open_book()
        page = book[REQ_SHEET]
        columns = _header_map(page)
        at = page.max_row + 1
        while at > 2 and not any(page.cell(row=at - 1, column=c).value
                                 for c in columns.values()):
            at -= 1
        for header, col in columns.items():
            if header in values:
                page.cell(row=at, column=col).value = values[header]
        book.save(store.XLSX_FILE)
        book.close()
    return at


def save_req(req_id, values, people):
    """Update one req and replace its whole candidate list.

    people is a list of {"Name", "Status", "Struck", "Highlight"} dicts.
    """
    with _store_lock():
        book = open_book()
        page = book[REQ_SHEET]

        columns = _header_map(page)
        for row in _rows_of(page, REQ_HEADERS):
            if row["Req ID"] != req_id:
                continue
            for header, col in columns.items():
                if header in values:
                    page.cell(row=row["_row"], column=col).value = values[header]
            break

        cands = book[CAND_SHEET]
        for row in sorted(_rows_of(cands, CAND_HEADERS),
                          key=lambda r: r["_row"], reverse=True):
            if row["Req ID"] == req_id:
                cands.delete_rows(row["_row"], 1)

        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        cand_cols = _header_map(cands)
        at = cands.max_row + 1
        for person in people:
            name = (person.get("Name") or "").strip()
            status = (person.get("Status") or "").strip()
            if not name and not status:
                continue
            for header, value in (("Req ID", req_id), ("Name", name), ("Status", status),
                                  ("TA", (person.get("TA") or "").strip()),
                                  ("Struck", "yes" if person.get("Struck") else ""),
                                  ("Highlight", "green" if person.get("Highlight") else ""),
                                  ("Added", stamp)):
                col = cand_cols.get(header)
                if col:
                    cands.cell(row=at, column=col).value = value
            at += 1

        book.save(store.XLSX_FILE)
        book.close()


def delete_req(req_id):
    with _store_lock():
        book = open_book()
        page = book[REQ_SHEET]
        for row in sorted(_rows_of(page, REQ_HEADERS), key=lambda r: r["_row"], reverse=True):
            if row["Req ID"] == req_id:
                page.delete_rows(row["_row"], 1)
        cands = book[CAND_SHEET]
        for row in sorted(_rows_of(cands, CAND_HEADERS), key=lambda r: r["_row"], reverse=True):
            if row["Req ID"] == req_id:
                cands.delete_rows(row["_row"], 1)
        book.save(store.XLSX_FILE)
        book.close()


_PAGE_CACHE = {}


def logo_exists():
    import app
    return app.logo_exists()


def openreqs_template():
    """Built once, from app.py's shared STYLE and HEADER, so the nav never drifts."""
    if "page" not in _PAGE_CACHE:
        import app
        _PAGE_CACHE["page"] = (_PAGE_BODY
                               .replace("{STYLE}", app.STYLE)
                               .replace("{HEADER}", app.HEADER))
    return _PAGE_CACHE["page"]


_PAGE_BODY = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Hire2o - Open Reqs</title>
{STYLE}
</head>
<body>
{HEADER}
<div class="shell">

  <div class="panel panel-pad">
    <form method="post" action="/reqs/add" class="reqform">
      <div class="exprow">
        <div class="expfield narrow">
          <label class="fieldlabel">Priority</label>
          <div style="padding:6px 0 2px;">
            <input type="hidden" name="priority" id="newprio" value="Blue">
            <button type="button" class="prio prio-blue" data-colour="blue"
                    title="Blue - Cold - click to change"
                    onclick="cycleNew(this)"></button>
          </div>
        </div>
        <div class="expfield narrow">
          <label class="fieldlabel" for="qty">Qty</label>
          <select id="qty" name="qty">
            {% for n in qty_choices %}<option value="{{ n }}">{{ n }}</option>{% endfor %}
          </select>
        </div>
        <div class="expfield wide">
          <label class="fieldlabel" for="client">Client</label>
          <input type="text" id="client" name="client" list="clientlist" required
                 placeholder="who the req is for">
          <datalist id="clientlist">
            {% for c in clients %}<option value="{{ c }}">{% endfor %}
          </datalist>
        </div>
        <div class="expfield wide">
          <label class="fieldlabel" for="req">Req</label>
          <input type="text" id="req" name="req" placeholder="role or req number">
        </div>
        <div class="expfield auth">
          <label class="fieldlabel" for="work_auth">Work Auth</label>
          <select id="work_auth" name="work_auth">
            {% for w in workauth_choices %}<option value="{{ w }}">{{ w }}</option>{% endfor %}
          </select>
        </div>
        <div class="expfield">
          <label class="fieldlabel" for="date">Date</label>
          <input type="date" id="date" name="date" value="{{ today }}">
        </div>
        <div class="expfield rate">
          <label class="fieldlabel" for="rate">Rate</label>
          <input type="text" id="rate" name="rate" placeholder="$75/hr">
        </div>
        <div class="expfield wide">
          <label class="fieldlabel" for="location">Location</label>
          <input type="text" id="location" name="location" placeholder="city, state">
        </div>
        <div class="expfield">
          <label class="fieldlabel" for="relocation">Relocation</label>
          <select id="relocation" name="relocation">
            {% for r in relocation_choices %}<option value="{{ r }}">{{ r }}</option>{% endfor %}
          </select>
        </div>
        <div class="expfield">
          <label class="fieldlabel" for="type">Type</label>
          <select id="type" name="type">
            {% for t in type_choices %}<option value="{{ t }}">{{ t }}</option>{% endfor %}
          </select>
        </div>
        <div class="expfield">
          <label class="fieldlabel">&nbsp;</label>
          <button class="btn btn-primary" type="submit">+ Add req</button>
        </div>
      </div>
    </form>
  </div>

  <div class="panel panel-pad" style="margin-top:16px;">
    <form method="get" action="/reqs">
      <div class="searchrow">
        <div>
          <label class="fieldlabel" for="q">Search</label>
          <input type="text" id="q" name="q" value="{{ q }}"
                 placeholder="client, location, candidate, status...">
        </div>
        <div>
          <label class="fieldlabel" for="fclient">Client</label>
          <select id="fclient" name="client" onchange="this.form.submit()">
            <option value="">All clients</option>
            {% for c in clients %}
              <option value="{{ c }}" {% if c == client %}selected{% endif %}>{{ c }}</option>
            {% endfor %}
          </select>
        </div>
        <div>
          <label class="fieldlabel" for="ftype">Type</label>
          <select id="ftype" name="type" onchange="this.form.submit()">
            <option value="">All types</option>
            {% for t in type_choices %}
              <option value="{{ t }}" {% if t == kind %}selected{% endif %}>{{ t }}</option>
            {% endfor %}
          </select>
        </div>
        <div>
          <label class="fieldlabel" for="fprio">Priority</label>
          <select id="fprio" name="priority" onchange="this.form.submit()">
            <option value="">All</option>
            {% for c in colours %}
              <option value="{{ c }}" {% if c == prio %}selected{% endif %}>
                {{ c | title }} - {{ meaning[c] }}
              </option>
            {% endfor %}
          </select>
        </div>
        <div style="align-self:flex-end; display:flex; gap:8px; align-items:center;">
          <button class="btn" type="submit">Search</button>
          <button class="btn" type="button" onclick="openAnalytics()">&#128202; Analytics</button>
          {% if q or client or kind or prio %}
            <a href="/reqs" style="font-size:13px; color:var(--ink-soft);">Clear</a>
          {% endif %}
        </div>
      </div>
    </form>
  </div>

  <div class="sectionhead">
    <h2>Open reqs</h2>
    <span class="counts">showing {{ reqs|length }} of {{ total }}</span>
    <span class="chip">{{ openings }} openings</span>
    <span class="chip chip-soft">{{ submitted }} submitted</span>
    <span class="legend">
      {% for c in colours %}
        <span><i class="dot prio-{{ c }}"></i>{{ meaning[c] }}</span>
      {% endfor %}
    </span>
    {% if saved and saved != "0" %}<span class="savedflash">Saved {{ saved }} req{% if saved != "1" %}s{% endif %}</span>{% elif saved == "0" %}<span class="savestate">Nothing had changed</span>{% endif %}
    <span class="savestate" id="savestate">No unsaved changes</span>

    <span class="sortbar">
      <span style="font-size:12px; color:var(--ink-faint);">Sort:</span>
      {% for field in sort_fields %}
        <a class="sortbtn {% if sort == field %}on{% endif %}"
           href="/reqs?sort={{ field }}&dir={% if sort == field and direction == 'asc' %}desc{% else %}asc{% endif %}{% if q %}&q={{ q }}{% endif %}{% if client %}&client={{ client }}{% endif %}{% if kind %}&type={{ kind }}{% endif %}{% if prio %}&priority={{ prio }}{% endif %}">
          {{ field }}{% if sort == field %} {% if direction == 'asc' %}&uarr;{% else %}&darr;{% endif %}{% endif %}
        </a>
      {% endfor %}
      {% if sort %}
        <a class="sortbtn clear"
           href="/reqs{% if q %}?q={{ q }}{% endif %}">clear</a>
      {% endif %}
    </span>
  </div>

  {% if reqs %}
    <form method="post" action="/reqs/save" id="reqsave">
      <input type="hidden" name="q" value="{{ q }}">
      <input type="hidden" name="sort" value="{{ sort }}">
      <input type="hidden" name="dir" value="{{ direction }}">

    {% for req in reqs %}
      {% set rid = req['Req ID'] %}
      <div class="reqcard">
        <input type="hidden" name="req_ids" value="{{ rid }}">

        <div class="reqtop">
          <input type="hidden" name="r_{{ rid }}_priority" value="{{ req.colour | priolabel }}">
          <button type="button" class="prio prio-{{ req.colour }}" data-colour="{{ req.colour }}"
                  title="{{ req.colour | priolabel }} - {{ meaning[req.colour] }} - click to change"
                  onclick="cycleCard(this)"></button>

          <div class="reqfield qty">
            <label class="fieldlabel">Qty</label>
            <select name="r_{{ rid }}_qty" onchange="markDirty()">
              {% for n in qty_choices %}
                <option value="{{ n }}" {% if n == req.Qty %}selected{% endif %}>{{ n }}</option>
              {% endfor %}
            </select>
          </div>
          <div class="reqfield grow">
            <label class="fieldlabel">Client</label>
            <input type="text" name="r_{{ rid }}_client" value="{{ req.Client }}"
                   list="clientlist" oninput="markDirty()">
          </div>
          <div class="reqfield grow">
            <label class="fieldlabel">Req</label>
            <input type="text" name="r_{{ rid }}_req" value="{{ req.Req }}"
                   placeholder="role or req number" oninput="markDirty()">
          </div>
          <div class="reqfield auth">
            <label class="fieldlabel">Work Auth</label>
            <select name="r_{{ rid }}_work_auth" onchange="markDirty()">
              {% for w in workauth_choices %}
                <option value="{{ w }}" {% if w == req['Work Auth'] %}selected{% endif %}>{{ w }}</option>
              {% endfor %}
            </select>
          </div>
          <div class="reqfield">
            <label class="fieldlabel">Date</label>
            <input type="date" name="r_{{ rid }}_date" value="{{ req.Date }}" onchange="markDirty()">
          </div>
          <div class="reqfield rate">
            <label class="fieldlabel">Rate</label>
            <input type="text" name="r_{{ rid }}_rate" value="{{ req.Rate }}" oninput="markDirty()">
          </div>
          <div class="reqfield grow">
            <label class="fieldlabel">Location</label>
            <input type="text" name="r_{{ rid }}_location" value="{{ req.Location }}" oninput="markDirty()">
          </div>
          <div class="reqfield">
            <label class="fieldlabel">Relocation</label>
            <select name="r_{{ rid }}_relocation" onchange="markDirty()">
              {% for r in relocation_choices %}
                <option value="{{ r }}" {% if r == req.Relocation %}selected{% endif %}>{{ r }}</option>
              {% endfor %}
            </select>
          </div>
          <div class="reqfield">
            <label class="fieldlabel">Type</label>
            <select name="r_{{ rid }}_type" onchange="markDirty()">
              {% for t in type_choices %}
                <option value="{{ t }}" {% if t == req.Type %}selected{% endif %}>{{ t }}</option>
              {% endfor %}
            </select>
          </div>
        </div>

        <div class="reqpeople">
          <div class="peoplehead">
            <span class="fieldlabel">Candidates</span>
            <span class="peoplecount">{{ req.candidates|length }} of {{ req.Qty or '?' }}</span>
            <button type="button" class="btn btn-sm addname" onclick="addName(this)">+ Add name</button>
            <button class="btn btn-primary btn-sm" type="submit"
                    title="Saves every req you have changed on this page">&#128190; Save</button>
            <span class="reqid">{{ rid }}</span>
            <button class="btn btn-danger btn-sm" type="submit" name="delete_id" value="{{ rid }}"
                    formaction="/reqs/delete"
                    onclick="return confirm('Delete this req and its {{ req.candidates|length }} candidate line(s)?');">
              Delete
            </button>
          </div>

          <div class="namelist">
            {% for cand in req.candidates %}
              <div class="namerow{% if cand.struck %} struck{% endif %}{% if cand.green %} green{% endif %}">
                <input type="text" name="c_{{ rid }}_name" class="name20" value="{{ cand.Name }}"
                       placeholder="Name" maxlength="60" oninput="markDirty()">
                <input type="text" name="c_{{ rid }}_status" class="status60" value="{{ cand.Status }}"
                       placeholder="Status" maxlength="200" oninput="markDirty()">
                <select name="c_{{ rid }}_ta" class="ta2" title="TA" onchange="markDirty()">
                  {% for t in ta_choices %}
                    <option value="{{ t }}" {% if t == cand.ta %}selected{% endif %}>{{ t or '-' }}</option>
                  {% endfor %}
                </select>
                <input type="hidden" name="c_{{ rid }}_struck" value="{% if cand.struck %}1{% endif %}">
                <input type="hidden" name="c_{{ rid }}_green" value="{% if cand.green %}1{% endif %}">
                <button type="button" class="rowmark strike{% if cand.struck %} on{% endif %}"
                        title="Strike this candidate out" onclick="toggleStrike(this)">S</button>
                <button type="button" class="rowmark tint{% if cand.green %} on{% endif %}"
                        title="Shade the name box green" onclick="toggleGreen(this)">&#9679;</button>
                <button type="button" class="rowx" title="Remove this line"
                        onclick="dropName(this)">&times;</button>
              </div>
            {% endfor %}
            {% if not req.candidates %}
              <p class="nonames">No names yet - press <b>+ Add name</b>.</p>
            {% endif %}
          </div>
        </div>
      </div>
    {% endfor %}
    </form>

    <p class="muted" style="font-size:12.5px; margin-top:14px;">
      <b>{{ workbook }}</b> &rarr; sheets <b>{{ req_sheet }}</b> and <b>{{ cand_sheet }}</b>.
      One Save writes every req you have changed.
    </p>
  {% else %}
    <div class="panel empty">
      <p>{% if q or client or kind or prio %}Nothing matches that filter.{% else %}No open reqs yet.{% endif %}</p>
      <p style="font-size:13px;">Fill in the form at the top to add the first one.</p>
    </div>
  {% endif %}

  <div class="jmodal" id="amodal" onclick="if(event.target===this){closeAnalytics();}">
    <div class="jcard" role="dialog" aria-label="Open reqs analytics">
      <div class="jhead">
        <div>
          <h2>Open reqs analytics</h2>
          <div class="jsub" id="asub"></div>
        </div>
        <button type="button" class="jclose" onclick="closeAnalytics()" aria-label="Close">&times;</button>
      </div>
      <div class="jbody" id="abody"></div>
    </div>
  </div>

  <script>
    var dirty = false;
    function markDirty() {
      dirty = true;
      var s = document.getElementById('savestate');
      if (s) { s.textContent = 'Unsaved changes'; s.classList.add('dirty'); }
    }
    document.addEventListener('DOMContentLoaded', function () {
      var form = document.getElementById('reqsave');
      if (form) { form.addEventListener('submit', function () { dirty = false; }); }
      window.addEventListener('beforeunload', function (e) {
        if (dirty) { e.preventDefault(); e.returnValue = ''; }
      });
    });

    var TA_OPTIONS = {{ ta_choices | tojson }}.map(function (t) {
      return '<option value="' + t + '">' + (t || '-') + '</option>';
    }).join('');

    var PRIORITY = [['blue', 'Blue', 'Cold'], ['red', 'Red', 'Hot'],
                    ['yellow', 'Yellow', 'Warm'], ['green', 'Green', 'Room Temp']];

    function nextColour(btn) {
      var now = btn.getAttribute('data-colour') || 'blue';
      var at = 0;
      for (var i = 0; i < PRIORITY.length; i++) { if (PRIORITY[i][0] === now) { at = i; } }
      var next = PRIORITY[(at + 1) % PRIORITY.length];
      btn.setAttribute('data-colour', next[0]);
      btn.className = 'prio prio-' + next[0];
      btn.title = next[1] + ' - ' + next[2] + ' - click to change';
      return next;
    }

    function cycleNew(btn) {
      document.getElementById('newprio').value = nextColour(btn)[1];
    }

    function cycleCard(btn) {
      var next = nextColour(btn);
      var hidden = btn.parentNode.querySelector('input[type=hidden]');
      if (hidden) { hidden.value = next[1]; }
      markDirty();
    }


    // ---- analytics -----------------------------------------------------------
    var STAGE_COLOUR = {
      'Submitted': '#6c5ce7', 'Screening': '#00a8a8', 'Interview': '#f59e0b',
      'Offer': '#16a34a', 'Placed': '#0f2f5c', 'Rejected': '#c4cbd6', 'Other': '#8b9099'
    };
    var PRIO_COLOUR = { blue: '#2563eb', red: '#dc2626', yellow: '#eab308', green: '#16a34a' };

    function aEsc(s) {
      return String(s).replace(/[&<>"]/g, function (c) {
        return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
      });
    }

    function openAnalytics() {
      var modal = document.getElementById('amodal');
      var body = document.getElementById('abody');
      body.innerHTML = '<p class="jempty">Counting...</p>';
      document.getElementById('asub').textContent = '';
      modal.classList.add('open');

      fetch('/reqs/analytics' + window.location.search)
        .then(function (r) { return r.json(); })
        .then(function (data) { drawAnalytics(data, body); })
        .catch(function (err) {
          body.innerHTML = '<p class="jempty">Could not load: ' + aEsc(err) + '</p>';
        });
    }

    function closeAnalytics() {
      document.getElementById('amodal').classList.remove('open');
    }

    function bar(label, value, most, colour, right, title) {
      var pct = most ? (value / most) * 100 : 0;
      return '<div class="abar"' + (title ? ' title="' + aEsc(title) + '"' : '') + '>' +
        '<span class="alabel">' + aEsc(label) + '</span>' +
        '<span class="atrack"><i class="afill" style="width:' + pct +
          '%;background:' + colour + '"></i></span>' +
        '<span class="acount">' + right + '</span></div>';
    }

    function card(title, note, inner) {
      return '<section class="acard"><h3>' + aEsc(title) +
             (note ? '<span>' + aEsc(note) + '</span>' : '') + '</h3>' + inner + '</section>';
    }

    function drawAnalytics(data, host) {
      var t = data.totals;
      document.getElementById('asub').textContent =
        t.reqs + ' reqs, ' + t.clients + ' client' + (t.clients === 1 ? '' : 's') +
        (window.location.search ? ' - matching the current filter' : '');

      if (!t.reqs) { host.innerHTML = '<p class="jempty">No reqs to chart.</p>'; return; }

      var html = '<div class="jstats">' +
        '<div class="jstat"><span>Openings</span><b>' + t.openings + '</b></div>' +
        '<div class="jstat"><span>Live candidates</span><b>' + t.submitted + '</b></div>' +
        '<div class="jstat"><span>Per opening</span><b>' +
           (t.openings ? (t.submitted / t.openings).toFixed(1) : '--') + '</b></div>' +
        '<div class="jstat"><span>Reqs with nobody</span><b class="' +
           (t.uncovered ? 'warn' : '') + '">' + t.uncovered + '</b></div>' +
        '<div class="jstat"><span>Oldest req</span><b>' + t.oldest + 'd</b></div>' +
        '</div><div class="agrid">';

      // 1. coverage by client
      var most = 1;
      data.clients.forEach(function (c) { most = Math.max(most, c.openings, c.submitted); });
      var inner = '';
      data.clients.forEach(function (c) {
        var cover = c.openings ? Math.round((c.submitted / c.openings) * 100) : 0;
        inner += '<div class="apair">' +
          '<div class="apairhead">' + aEsc(c.client) +
            '<b class="' + (cover >= 100 ? 'ok' : cover === 0 ? 'warn' : '') + '">' +
            cover + '%</b></div>' +
          bar('openings', c.openings, most, '#c7d2e0', String(c.openings)) +
          bar('candidates', c.submitted, most, '#00a8a8', String(c.submitted)) +
          (c.uncovered ? '<p class="anote">' + c.uncovered + ' req' +
             (c.uncovered === 1 ? '' : 's') + ' with nobody on it</p>' : '') +
          '</div>';
      });
      html += card('Coverage by client', 'candidates against openings', inner);

      // 2. the pipeline
      var stageMost = 1;
      data.stages.forEach(function (p) { stageMost = Math.max(stageMost, p[1]); });
      inner = '';
      data.stages.forEach(function (p) {
        inner += bar(p[0], p[1], stageMost, STAGE_COLOUR[p[0]] || '#8b9099', String(p[1]));
      });
      html += card('Pipeline', 'live candidates by status',
                   inner || '<p class="anote">No candidates yet.</p>');

      // 3. load per TA, split by stage
      inner = '';
      var taMost = 1;
      data.tas.forEach(function (ta) { taMost = Math.max(taMost, ta.total); });
      data.tas.forEach(function (ta) {
        var segs = '';
        data.stage_order.forEach(function (stage) {
          var n = ta.stages[stage] || 0;
          if (!n) { return; }
          segs += '<i style="width:' + ((n / taMost) * 100) + '%;background:' +
                  STAGE_COLOUR[stage] + '" title="' + stage + ': ' + n + '"></i>';
        });
        inner += '<div class="abar"><span class="alabel">' + aEsc(ta.ta) + '</span>' +
                 '<span class="atrack stacked">' + segs + '</span>' +
                 '<span class="acount">' + ta.total + '</span></div>';
      });
      html += card('Load by TA', 'segments are pipeline stages',
                   inner || '<p class="anote">Nobody assigned yet.</p>');

      // 4. priority
      inner = '';
      var prioMost = 1;
      data.priority.forEach(function (p) { prioMost = Math.max(prioMost, p.openings, p.reqs); });
      data.priority.forEach(function (p) {
        inner += '<div class="abar"><span class="alabel">' +
          '<i class="dot prio-' + p.name + '"></i>' + aEsc(p.meaning) + '</span>' +
          '<span class="atrack"><i class="afill" style="width:' +
            ((p.openings / prioMost) * 100) + '%;background:' + PRIO_COLOUR[p.name] + '"></i></span>' +
          '<span class="acount">' + p.openings +
            '<i class="adrop">' + p.reqs + ' req' + (p.reqs === 1 ? '' : 's') + '</i></span></div>';
      });
      html += card('Openings by priority', '', inner);

      // 5. ageing
      inner = '';
      var ageMost = 1;
      data.ageing.forEach(function (r) { ageMost = Math.max(ageMost, r.days); });
      data.ageing.forEach(function (r) {
        inner += '<div class="abar"' +
          ' title="' + aEsc(r.client) + ' - ' + r.submitted + ' of ' + r.qty + ' covered">' +
          '<span class="alabel">' + aEsc(r.label) + '</span>' +
          '<span class="atrack"><i class="afill" style="width:' +
            ((r.days / ageMost) * 100) + '%;background:' +
            (r.submitted ? '#c7d2e0' : '#dc2626') + '"></i></span>' +
          '<span class="acount">' + r.days + 'd' +
            '<i class="adrop">' + r.submitted + '/' + r.qty + '</i></span></div>';
      });
      html += card('Open longest', 'red means nobody submitted yet',
                   inner || '<p class="anote">No dated reqs.</p>');

      // 6. the mix
      inner = '<div class="amix"><h4>Type</h4>';
      data.types.forEach(function (r) {
        inner += '<span class="chip chip-soft">' + aEsc(r.name) + ' ' + r.openings + '</span>';
      });
      inner += '</div><div class="amix"><h4>Work auth</h4>';
      data.auth.forEach(function (r) {
        inner += '<span class="chip chip-soft">' + aEsc(r.name) + ' ' + r.reqs + '</span>';
      });
      inner += '</div>';
      html += card('Mix', 'openings by type, reqs by work auth', inner);

      html += '</div>';

      // the per-req detail, kept at the end
      var detail = '';
      data.clients.forEach(function (c) {
        detail += '<div class="agroup"><div class="ahead">' + aEsc(c.client) +
                  '<span>' + c.submitted + ' live / ' + c.openings + ' open</span></div>';
        var reqMost = 1;
        c.reqs.forEach(function (r) { reqMost = Math.max(reqMost, r.submitted, r.qty); });
        c.reqs.forEach(function (r) {
          var pct = (r.submitted / reqMost) * 100, target = (r.qty / reqMost) * 100;
          var full = r.qty && r.submitted >= r.qty;
          detail += '<div class="abar"><span class="alabel" title="' + aEsc(r.label) + '">' +
            aEsc(r.label) + '</span><span class="atrack">' +
            '<i class="afill' + (full ? ' full' : '') + '" style="width:' + pct + '%"></i>' +
            (r.qty ? '<i class="atarget" style="left:' + target + '%"></i>' : '') +
            '</span><span class="acount">' + r.submitted +
            (r.qty ? '<em>/' + r.qty + '</em>' : '') +
            (r.dropped ? '<i class="adrop">+' + r.dropped + ' out</i>' : '') +
            '</span></div>';
        });
        detail += '</div>';
      });
      html += '<details class="adetail"><summary>Every req, one bar each</summary>' +
              detail + '</details>';

      html += '<p class="jempty" style="padding:10px 2px 0;">' +
              'Struck-out candidates are left out of every count.</p>';
      host.innerHTML = html;
    }

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') { closeAnalytics(); }
    });

    function addName(btn) {
      var card = btn.closest('.reqcard');
      var rid = card.querySelector('input[name=req_ids]').value;
      var list = card.querySelector('.namelist');

      var empty = list.querySelector('.nonames');
      if (empty) { empty.remove(); }

      var row = document.createElement('div');
      row.className = 'namerow';
      row.innerHTML =
        '<input type="text" class="name20" placeholder="Name" maxlength="60"' +
        ' name="c_' + rid + '_name" oninput="markDirty()">' +
        '<input type="text" class="status60" placeholder="Status" maxlength="200"' +
        ' name="c_' + rid + '_status" oninput="markDirty()">' +
        '<select name="c_' + rid + '_ta" class="ta2" title="TA" onchange="markDirty()">' +
        TA_OPTIONS + '</select>' +
        '<input type="hidden" name="c_' + rid + '_struck" value="">' +
        '<input type="hidden" name="c_' + rid + '_green" value="">' +
        '<button type="button" class="rowmark strike" title="Strike this candidate out"' +
        ' onclick="toggleStrike(this)">S</button>' +
        '<button type="button" class="rowmark tint" title="Shade the name box green"' +
        ' onclick="toggleGreen(this)">&#9679;</button>' +
        '<button type="button" class="rowx" title="Remove this line"' +
        ' onclick="dropName(this)">&times;</button>';
      list.appendChild(row);
      row.querySelector('input').focus();
    }

    function toggleMark(btn, cssClass, field) {
      var row = btn.closest('.namerow');
      var on = !row.classList.contains(cssClass);
      row.classList.toggle(cssClass, on);
      btn.classList.toggle('on', on);
      var hidden = row.querySelector('input[name$="_' + field + '"]');
      if (hidden) { hidden.value = on ? '1' : ''; }
      markDirty();
    }

    function toggleStrike(btn) { toggleMark(btn, 'struck', 'struck'); }
    function toggleGreen(btn) { toggleMark(btn, 'green', 'green'); }

    function dropName(btn) {
      var list = btn.closest('.namelist');
      btn.closest('.namerow').remove();
      markDirty();
      if (!list.querySelector('.namerow')) {
        var note = document.createElement('p');
        note.className = 'nonames';
        note.innerHTML = 'No names yet - press <b>+ Add name</b>.';
        list.appendChild(note);
      }
    }
  </script>
</div>
</body>
</html>
"""


@bp.route("/reqs")
def reqs_page():
    reqs = read_reqs()

    q = request.args.get("q", "").strip()
    client = request.args.get("client", "").strip()
    kind = request.args.get("type", "").strip()
    prio = request.args.get("priority", "").strip()

    shown = filter_reqs(reqs, q, client, kind, prio)

    sort = request.args.get("sort", "").strip()
    direction = request.args.get("dir", "asc").strip()
    shown = sort_reqs(shown, sort, direction)

    return render_template_string(
        openreqs_template(),
        reqs=shown,
        total=len(reqs),
        openings=sum(r["_qty"] for r in shown),
        submitted=sum(len(r["candidates"]) for r in shown),
        clients=sorted({r["Client"] for r in reqs if r["Client"]}),
        qty_choices=QTY_CHOICES,
        relocation_choices=RELOCATION_CHOICES,
        workauth_choices=WORKAUTH_CHOICES,
        ta_choices=list(TA_CHOICES),
        type_choices=TYPE_CHOICES,
        colours=list(PRIORITY_COLOURS),
        meaning=PRIORITY_MEANING,
        q=q, client=client, kind=kind, prio=prio,
        sort=sort, direction=direction,
        sort_fields=list(FIELD_ORDER),
        today=datetime.now().date().isoformat(),
        workbook=store.XLSX_FILE,
        req_sheet=REQ_SHEET,
        cand_sheet=CAND_SHEET,
        saved=request.args.get("saved", ""),
        has_logo=logo_exists(),
        notify_status=notify.status_line(),
        tab="reqs",
    )


# candidate statuses are free text, so bucket them into a pipeline
PIPELINE = (
    ("Placed",     ("placed", "started", "joined", "onboard", "on board", "billing")),
    ("Offer",      ("offer", "selected", "accept")),
    ("Interview",  ("interview", "client round", "round ", "f2f", "panel", "loop")),
    ("Screening",  ("screen", "prescreen", "phone", "qualif", "intake", "review")),
    ("Submitted",  ("submit", "sent", "shared", "presented", "profile")),
    ("Rejected",   ("reject", "declin", "drop", "not moving", "no go", "lost",
                    "backed out", "hold", "closed")),
)
STAGE_ORDER = ("Submitted", "Screening", "Interview", "Offer", "Placed", "Rejected", "Other")


def stage_of(status):
    low = " " + (status or "").strip().lower() + " "
    if not low.strip():
        return "Submitted"
    for name, words in PIPELINE:
        if any(w in low for w in words):
            return name
    return "Other"


def days_open(req, today=None):
    when = (req.get("Date") or "").strip()
    try:
        opened = datetime.strptime(when[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    return ((today or datetime.now().date()) - opened).days


@bp.route("/reqs/analytics")
def reqs_analytics():
    """Everything the analytics panel draws, counted once here."""
    reqs = filter_reqs(read_reqs(),
                       request.args.get("q", "").strip(),
                       request.args.get("client", "").strip(),
                       request.args.get("type", "").strip(),
                       request.args.get("priority", "").strip())

    def live(req):
        return [c for c in req["candidates"] if not c.get("struck")]

    # ---- per client, and per req inside it
    clients = {}
    for req in reqs:
        name = req["Client"] or "(no client)"
        bucket = clients.setdefault(name, {"client": name, "reqs": [],
                                           "submitted": 0, "openings": 0, "uncovered": 0})
        people = live(req)
        bucket["reqs"].append({
            "label": req["Req"] or req["Type"] or req["Req ID"][-4:],
            "submitted": len(people),
            "dropped": len(req["candidates"]) - len(people),
            "qty": req["_qty"],
            "colour": req["colour"],
        })
        bucket["submitted"] += len(people)
        bucket["openings"] += req["_qty"]
        if not people:
            bucket["uncovered"] += 1
    ordered = sorted(clients.values(), key=lambda c: (-c["openings"], c["client"]))
    for bucket in ordered:
        bucket["reqs"].sort(key=lambda r: -r["submitted"])

    # ---- the pipeline, and each TA's share of it
    stages = {name: 0 for name in STAGE_ORDER}
    tas = {}
    for req in reqs:
        for cand in live(req):
            stage = stage_of(cand.get("Status", ""))
            stages[stage] = stages.get(stage, 0) + 1
            who = cand.get("ta") or "unassigned"
            row = tas.setdefault(who, {"ta": who, "total": 0,
                                       "stages": {n: 0 for n in STAGE_ORDER}})
            row["total"] += 1
            row["stages"][stage] += 1

    # ---- reqs and openings by priority, type and work auth
    def tally(key):
        out = {}
        for req in reqs:
            name = (req.get(key) or "(blank)") if key != "colour" else req["colour"]
            row = out.setdefault(name, {"name": name, "reqs": 0, "openings": 0, "submitted": 0})
            row["reqs"] += 1
            row["openings"] += req["_qty"]
            row["submitted"] += len(live(req))
        return out

    priority = [dict(tally("colour").get(c, {"name": c, "reqs": 0, "openings": 0, "submitted": 0}))
                for c in PRIORITY_COLOURS]
    for row in priority:
        row["meaning"] = PRIORITY_MEANING.get(row["name"], "")

    # ---- ageing: the reqs that have been open longest
    ageing = []
    for req in reqs:
        age = days_open(req)
        if age is None:
            continue
        people = live(req)
        ageing.append({"label": req["Req"] or req["Type"] or req["Req ID"][-4:],
                       "client": req["Client"], "days": age,
                       "submitted": len(people), "qty": req["_qty"],
                       "colour": req["colour"]})
    ageing.sort(key=lambda r: -r["days"])

    ages = [r["days"] for r in ageing]
    submitted_total = sum(len(live(r)) for r in reqs)
    openings_total = sum(r["_qty"] for r in reqs)

    return jsonify({
        "clients": ordered,
        "stages": [[name, stages.get(name, 0)] for name in STAGE_ORDER if stages.get(name, 0)],
        "tas": sorted(tas.values(), key=lambda t: -t["total"]),
        "priority": priority,
        "types": sorted(tally("Type").values(), key=lambda r: -r["openings"]),
        "auth": sorted(tally("Work Auth").values(), key=lambda r: -r["reqs"]),
        "ageing": ageing[:10],
        "stage_order": list(STAGE_ORDER),
        "totals": {
            "reqs": len(reqs),
            "clients": len(ordered),
            "submitted": submitted_total,
            "openings": openings_total,
            "uncovered": sum(1 for r in reqs if not live(r)),
            "oldest": max(ages) if ages else 0,
            "median_age": sorted(ages)[len(ages) // 2] if ages else 0,
        },
    })


@bp.route("/reqs/add", methods=["POST"])
def reqs_add():
    form = request.form
    add_req({
        "Req ID": datetime.now().strftime("R%Y%m%d%H%M%S%f"),
        "Priority": priority_label(priority_of(form.get("priority", ""))),
        "Qty": (form.get("qty", "") or "").strip(),
        "Client": (form.get("client", "") or "").strip(),
        "Req": (form.get("req", "") or "").strip(),
        "Work Auth": (form.get("work_auth", "") or "").strip(),
        "Date": (form.get("date", "") or "").strip(),
        "Rate": (form.get("rate", "") or "").strip(),
        "Location": (form.get("location", "") or "").strip(),
        "Relocation": (form.get("relocation", "") or "").strip(),
        "Type": (form.get("type", "") or "").strip(),
        "Added": datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    return redirect(url_for("openreqs.reqs_page", saved="1"))


FORM_FIELDS = (("priority", "Priority"), ("qty", "Qty"), ("client", "Client"),
               ("req", "Req"), ("work_auth", "Work Auth"), ("date", "Date"),
               ("rate", "Rate"), ("location", "Location"),
               ("relocation", "Relocation"), ("type", "Type"))


@bp.route("/reqs/save", methods=["POST"])
def reqs_save():
    """Write every req on the page whose values or candidates actually changed."""
    form = request.form
    before = {r["Req ID"]: r for r in read_reqs()}
    saved = 0

    for req_id in form.getlist("req_ids"):
        req_id = (req_id or "").strip()
        current = before.get(req_id)
        if not current:
            continue

        values = {}
        for field, header in FORM_FIELDS:
            posted = (form.get(f"r_{req_id}_{field}", "") or "").strip()
            if field == "priority":
                posted = priority_label(priority_of(posted))
            values[header] = posted

        names = form.getlist(f"c_{req_id}_name")
        statuses = form.getlist(f"c_{req_id}_status")
        tas = form.getlist(f"c_{req_id}_ta")
        struck = form.getlist(f"c_{req_id}_struck")
        green = form.getlist(f"c_{req_id}_green")

        people = []
        for i, name in enumerate(names):
            name = (name or "").strip()
            status = (statuses[i] if i < len(statuses) else "").strip()
            if not name and not status:
                continue
            people.append({
                "Name": name, "Status": status,
                "TA": (tas[i] if i < len(tas) else "").strip(),
                "Struck": bool((struck[i] if i < len(struck) else "").strip()),
                "Highlight": bool((green[i] if i < len(green) else "").strip()),
            })

        was = [{"Name": c.get("Name", ""), "Status": c.get("Status", ""),
                "TA": c.get("ta", ""),
                "Struck": c.get("struck", False), "Highlight": c.get("green", False)}
               for c in current["candidates"]]

        fields_changed = any(values[h] != current.get(h, "") for _, h in FORM_FIELDS)
        if not fields_changed and people == was:
            continue

        save_req(req_id, values, people)
        saved += 1

    return redirect(url_for("openreqs.reqs_page", saved=str(saved),
                            q=form.get("q", "") or None,
                            sort=form.get("sort", "") or None,
                            dir=form.get("dir", "") or None))


@bp.route("/reqs/delete", methods=["POST"])
def reqs_delete():
    req_id = (request.form.get("delete_id", "")
              or request.form.get("req_id", "") or "").strip()
    if req_id:
        delete_req(req_id)
    return redirect(url_for("openreqs.reqs_page"))


bp.add_app_template_filter(priority_label, "priolabel")