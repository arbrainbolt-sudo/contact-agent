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

from flask import Blueprint, request, redirect, url_for, render_template_string

import store
import notify

bp = Blueprint("openreqs", __name__)


REQ_SHEET = os.getenv("OPENREQ_SHEET", "OpenReqs")
CAND_SHEET = os.getenv("OPENREQ_CAND_SHEET", "OpenReqCandidates")

REQ_HEADERS = ("Req ID", "Priority", "Qty", "Client", "Req", "Work Auth", "Date",
               "Rate", "Location", "Relocation", "Type", "Added")
CAND_HEADERS = ("Req ID", "Name", "Status", "Added")

# the order the fields appear on screen, and what each sort button offers
FIELD_ORDER = ("Priority", "Qty", "Client", "Req", "Work Auth", "Date", "Rate",
               "Location", "Relocation", "Type")

QTY_CHOICES = [str(n) for n in range(1, 100)]
RELOCATION_CHOICES = ("Remote", "Hybrid", "On site")
WORKAUTH_CHOICES = ("OPT+", "H1+", "GC+", "USC Only")
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
        try:
            req["_qty"] = int(req.get("Qty") or 0)
        except ValueError:
            req["_qty"] = 0
    return reqs


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


def save_req(req_id, values, names, statuses):
    """Update one req and replace its whole candidate list."""
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
        for name, status in zip(names, statuses):
            name, status = (name or "").strip(), (status or "").strip()
            if not name and not status:
                continue
            for header, value in (("Req ID", req_id), ("Name", name),
                                  ("Status", status), ("Added", stamp)):
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
    {% if saved %}<span class="savedflash">Saved</span>{% endif %}

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
    {% for req in reqs %}
      <form method="post" action="/reqs/save" class="reqcard">
        <input type="hidden" name="req_id" value="{{ req['Req ID'] }}">
        <input type="hidden" name="q" value="{{ q }}">

        <div class="reqtop">
          <input type="hidden" name="priority" value="{{ req.colour | priolabel }}">
          <button type="button" class="prio prio-{{ req.colour }}" data-colour="{{ req.colour }}"
                  title="{{ req.colour | priolabel }} - {{ meaning[req.colour] }} - click to change"
                  onclick="cycleCard(this)"></button>

          <div class="reqfield qty">
            <label class="fieldlabel">Qty</label>
            <select name="qty">
              {% for n in qty_choices %}
                <option value="{{ n }}" {% if n == req.Qty %}selected{% endif %}>{{ n }}</option>
              {% endfor %}
            </select>
          </div>
          <div class="reqfield grow">
            <label class="fieldlabel">Client</label>
            <input type="text" name="client" value="{{ req.Client }}" list="clientlist">
          </div>
          <div class="reqfield grow">
            <label class="fieldlabel">Req</label>
            <input type="text" name="req" value="{{ req.Req }}" placeholder="role or req number">
          </div>
          <div class="reqfield auth">
            <label class="fieldlabel">Work Auth</label>
            <select name="work_auth">
              {% for w in workauth_choices %}
                <option value="{{ w }}" {% if w == req['Work Auth'] %}selected{% endif %}>{{ w }}</option>
              {% endfor %}
            </select>
          </div>
          <div class="reqfield">
            <label class="fieldlabel">Date</label>
            <input type="date" name="date" value="{{ req.Date }}">
          </div>
          <div class="reqfield rate">
            <label class="fieldlabel">Rate</label>
            <input type="text" name="rate" value="{{ req.Rate }}">
          </div>
          <div class="reqfield grow">
            <label class="fieldlabel">Location</label>
            <input type="text" name="location" value="{{ req.Location }}">
          </div>
          <div class="reqfield">
            <label class="fieldlabel">Relocation</label>
            <select name="relocation">
              {% for r in relocation_choices %}
                <option value="{{ r }}" {% if r == req.Relocation %}selected{% endif %}>{{ r }}</option>
              {% endfor %}
            </select>
          </div>
          <div class="reqfield">
            <label class="fieldlabel">Type</label>
            <select name="type">
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
          </div>

          <div class="namelist">
            {% for cand in req.candidates %}
              <div class="namerow">
                <input type="text" name="name" class="name20" value="{{ cand.Name }}"
                       placeholder="Name" maxlength="60">
                <input type="text" name="status" class="name20" value="{{ cand.Status }}"
                       placeholder="Status" maxlength="60">
                <button type="button" class="rowx" title="Remove this line"
                        onclick="dropName(this)">&times;</button>
              </div>
            {% endfor %}
            <div class="namerow">
              <input type="text" name="name" class="name20" placeholder="Name" maxlength="60">
              <input type="text" name="status" class="name20" placeholder="Status" maxlength="60">
              <button type="button" class="rowx" title="Remove this line"
                      onclick="dropName(this)">&times;</button>
            </div>
          </div>
        </div>

        <div class="reqfoot">
          <button class="btn btn-primary" type="submit">&#128190; Save req</button>
          <span class="reqid">{{ req['Req ID'] }}</span>
          <button class="btn btn-danger" type="submit" formaction="/reqs/delete"
                  onclick="return confirm('Delete this req and its {{ req.candidates|length }} candidate line(s)?');">
            Delete
          </button>
        </div>
      </form>
    {% endfor %}

    <p class="muted" style="font-size:12.5px; margin-top:14px;">
      <b>{{ workbook }}</b> &rarr; sheets <b>{{ req_sheet }}</b> and <b>{{ cand_sheet }}</b>.
      Each card saves on its own.
    </p>
  {% else %}
    <div class="panel empty">
      <p>{% if q or client or kind or prio %}Nothing matches that filter.{% else %}No open reqs yet.{% endif %}</p>
      <p style="font-size:13px;">Fill in the form at the top to add the first one.</p>
    </div>
  {% endif %}

  <script>
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
      var hidden = btn.parentNode.querySelector('input[name=priority]');
      if (hidden) { hidden.value = next[1]; }
    }

    function addName(btn) {
      var list = btn.closest('.reqpeople').querySelector('.namelist');
      var row = list.querySelector('.namerow').cloneNode(true);
      row.querySelectorAll('input').forEach(function (input) { input.value = ''; });
      list.appendChild(row);
      row.querySelector('input').focus();
    }

    function dropName(btn) {
      var list = btn.closest('.namelist');
      var row = btn.closest('.namerow');
      if (list.querySelectorAll('.namerow').length > 1) { row.remove(); }
      else { row.querySelectorAll('input').forEach(function (i) { i.value = ''; }); }
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

    shown = reqs
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
                fields += [cand.get("Name", ""), cand.get("Status", "")]
            return any(needle in f.lower() for f in fields)

        shown = [r for r in shown if hit(r)]

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


@bp.route("/reqs/save", methods=["POST"])
def reqs_save():
    form = request.form
    req_id = (form.get("req_id", "") or "").strip()
    if req_id:
        save_req(req_id, {
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
        }, form.getlist("name"), form.getlist("status"))
    return redirect(url_for("openreqs.reqs_page", saved="1",
                            q=form.get("q", "") or None))


@bp.route("/reqs/delete", methods=["POST"])
def reqs_delete():
    req_id = (request.form.get("req_id", "") or "").strip()
    if req_id:
        delete_req(req_id)
    return redirect(url_for("openreqs.reqs_page"))


bp.add_app_template_filter(priority_label, "priolabel")