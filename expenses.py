"""
Expenses tab - log spending, club a meeting's items under one group id,
and attach receipt images. Everything lands in the Expenses worksheet of the
same workbook the rest of the app uses.

Wired into app.py with:
    from expenses import bp as expenses_bp
    app.register_blueprint(expenses_bp)
"""

import os
import re
import contextlib
from datetime import datetime

from flask import Blueprint, request, redirect, url_for, render_template_string

import store
import notify

bp = Blueprint("expenses", __name__)


EXPENSE_SHEET = os.getenv("EXPENSE_SHEET", "Expenses")
EXPENSE_TYPES = ("Food", "Gas", "EZPass", "Gift", "Office supply")
EXPENSE_HEADERS = ("Expense ID", "Group ID", "Date", "Client", "Amount",
                   "Type", "Paid by", "Location", "Receipt", "Added")
RECEIPT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "receipts")
RECEIPT_TYPES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".pdf"}


def expense_book():
    """Open the workbook, making sure the Expenses sheet and its headers exist."""
    import openpyxl
    book = openpyxl.load_workbook(store.XLSX_FILE)
    if EXPENSE_SHEET not in book.sheetnames:
        page = book.create_sheet(EXPENSE_SHEET)
        for i, header in enumerate(EXPENSE_HEADERS, start=1):
            page.cell(row=1, column=i).value = header
        book.save(store.XLSX_FILE)
        print(f"created the {EXPENSE_SHEET} sheet in {store.XLSX_FILE}")
    else:
        page = book[EXPENSE_SHEET]
        if not (page.cell(row=1, column=1).value or "").strip():
            for i, header in enumerate(EXPENSE_HEADERS, start=1):
                page.cell(row=1, column=i).value = header
            book.save(store.XLSX_FILE)
    return book, page


def read_expenses():
    """Every expense row, newest first."""
    with _store_lock():
        book, page = expense_book()
        rows = []
        for r in range(2, page.max_row + 1):
            values = [page.cell(row=r, column=i + 1).value
                      for i in range(len(EXPENSE_HEADERS))]
            if not any(str(v or "").strip() for v in values):
                continue
            row = {"_row": r}
            for key, value in zip(EXPENSE_HEADERS, values):
                row[key] = str(value or "").strip()
            try:
                row["_amount"] = float(str(row["Amount"]).replace("$", "").replace(",", "") or 0)
            except ValueError:
                row["_amount"] = 0.0
            rows.append(row)
        book.close()
    rows.sort(key=lambda r: (r["Date"], r["_row"]), reverse=True)
    return rows


def add_expense(values):
    """Append one expense. Returns its row number."""
    with _store_lock():
        book, page = expense_book()
        at = page.max_row + 1
        while at > 2 and not any(page.cell(row=at - 1, column=i + 1).value
                                 for i in range(len(EXPENSE_HEADERS))):
            at -= 1                                   # reuse trailing blank rows
        for i, header in enumerate(EXPENSE_HEADERS, start=1):
            page.cell(row=at, column=i).value = values.get(header, "")
        book.save(store.XLSX_FILE)
        book.close()
    return at


def update_expense(excel_row, values):
    with _store_lock():
        book, page = expense_book()
        for i, header in enumerate(EXPENSE_HEADERS, start=1):
            if header in values:
                page.cell(row=excel_row, column=i).value = values[header]
        book.save(store.XLSX_FILE)
        book.close()


def delete_expense(excel_row):
    with _store_lock():
        book, page = expense_book()
        page.delete_rows(excel_row, 1)
        book.save(store.XLSX_FILE)
        book.close()


def next_group_id(rows, when=None):
    """A fresh id for clubbing one meeting's expenses: G-20261112-1."""
    day = (when or datetime.now().date()).strftime("%Y%m%d")
    stem = f"G-{day}-"
    used = []
    for row in rows:
        gid = row.get("Group ID", "")
        if gid.startswith(stem):
            try:
                used.append(int(gid[len(stem):]))
            except ValueError:
                pass
    return stem + str(max(used) + 1 if used else 1)


def save_receipt(upload, group_id, expense_id):
    """Store the uploaded receipt under static/receipts and return its filename."""
    if not upload or not upload.filename:
        return ""
    stem, ext = os.path.splitext(upload.filename)
    ext = ext.lower()
    if ext not in RECEIPT_TYPES:
        return ""
    os.makedirs(RECEIPT_DIR, exist_ok=True)
    safe_group = re.sub(r"[^A-Za-z0-9_-]", "", group_id) or "ungrouped"
    name = f"{safe_group}_{expense_id}{ext}"
    upload.save(os.path.join(RECEIPT_DIR, name))
    return name


def expense_groups(rows):
    """Rows bundled by Group ID, each with its own total."""
    order, bundles = [], {}
    for row in rows:
        key = row.get("Group ID", "") or f"__single_{row['_row']}"
        if key not in bundles:
            bundles[key] = {"id": row.get("Group ID", ""), "rows": [], "total": 0.0}
            order.append(key)
        bundles[key]["rows"].append(row)
        bundles[key]["total"] += row["_amount"]
    return [bundles[k] for k in order]



def _store_lock():
    """Reuse store's own write lock when it has one, so direct writes stay safe."""
    for name in ("_LOCK", "LOCK", "_lock", "lock"):
        candidate = getattr(store, name, None)
        if hasattr(candidate, "__enter__"):
            return candidate
    return contextlib.nullcontext()


_PAGE_BODY = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Hire2o - Expenses</title>
{STYLE}
</head>
<body>
{HEADER}
<div class="shell">

  <div class="panel panel-pad">
    <form method="post" action="/expenses/add" enctype="multipart/form-data">
      <div class="exprow">
        <div class="expfield">
          <label class="fieldlabel" for="date">Date</label>
          <input type="date" id="date" name="date" value="{{ today }}" required>
        </div>
        <div class="expfield wide">
          <label class="fieldlabel" for="client">Client</label>
          <input type="text" id="client" name="client" list="clientlist" placeholder="who it was for">
          <datalist id="clientlist">
            {% for c in clients %}<option value="{{ c }}">{% endfor %}
          </datalist>
        </div>
        <div class="expfield narrow">
          <label class="fieldlabel" for="amount">Amount</label>
          <input type="text" inputmode="decimal" id="amount" name="amount" placeholder="0.00" required>
        </div>
        <div class="expfield">
          <label class="fieldlabel" for="type">Type</label>
          <select id="type" name="type">
            {% for t in types %}<option value="{{ t }}">{{ t }}</option>{% endfor %}
          </select>
        </div>
        <div class="expfield">
          <label class="fieldlabel" for="paid_by">Paid by</label>
          <input type="text" id="paid_by" name="paid_by" list="payerlist" placeholder="card, cash...">
          <datalist id="payerlist">
            {% for p in payers %}<option value="{{ p }}">{% endfor %}
          </datalist>
        </div>
        <div class="expfield wide">
          <label class="fieldlabel" for="location">Location</label>
          <input type="text" id="location" name="location" placeholder="where">
        </div>
      </div>

      <div class="exprow" style="margin-top:12px; align-items:flex-end;">
        <div class="expfield wide">
          <label class="fieldlabel" for="group_id">Group ID - club one meeting's expenses together</label>
          <input type="text" id="group_id" name="group_id" list="grouplist"
                 value="{{ group or '' }}" placeholder="leave blank for a one-off">
          <datalist id="grouplist">
            {% for g in group_ids %}<option value="{{ g }}">{% endfor %}
          </datalist>
        </div>
        <div class="expfield">
          <label class="fieldlabel">&nbsp;</label>
          <button class="btn" type="button"
                  onclick="document.getElementById('group_id').value='{{ suggested_group }}';">
            New group
          </button>
        </div>
        <div class="expfield wide">
          <label class="fieldlabel" for="receipt">&#128206; Receipt image</label>
          <input type="file" id="receipt" name="receipt" accept="image/*,.pdf" capture="environment">
        </div>
        <div class="expfield">
          <label class="fieldlabel">&nbsp;</label>
          <label class="checkline">
            <input type="checkbox" name="stay" value="1" {% if group %}checked{% endif %}>
            stay on this group
          </label>
        </div>
        <div class="expfield">
          <label class="fieldlabel">&nbsp;</label>
          <button class="btn btn-primary" type="submit">+ Add expense</button>
        </div>
      </div>
    </form>
  </div>

  <div class="panel panel-pad" style="margin-top:16px;">
    <form method="get" action="/expenses">
      <div class="searchrow">
        <div>
          <label class="fieldlabel" for="q">Search</label>
          <input type="text" id="q" name="q" value="{{ q }}" placeholder="client, location, group...">
        </div>
        <div>
          <label class="fieldlabel" for="ftype">Type</label>
          <select id="ftype" name="type" onchange="this.form.submit()">
            <option value="">All types</option>
            {% for t in types %}<option value="{{ t }}" {% if t == kind %}selected{% endif %}>{{ t }}</option>{% endfor %}
          </select>
        </div>
        <div>
          <label class="fieldlabel" for="fgroup">Group</label>
          <select id="fgroup" name="group" onchange="this.form.submit()">
            <option value="">All groups</option>
            {% for g in group_ids %}<option value="{{ g }}" {% if g == group %}selected{% endif %}>{{ g }}</option>{% endfor %}
          </select>
        </div>
        <div>
          <label class="fieldlabel" for="fmonth">Month</label>
          <select id="fmonth" name="month" onchange="this.form.submit()">
            <option value="">All months</option>
            {% for m in months %}<option value="{{ m }}" {% if m == month %}selected{% endif %}>{{ m }}</option>{% endfor %}
          </select>
        </div>
        <div style="align-self:flex-end; display:flex; gap:8px; align-items:center;">
          <button class="btn" type="submit">Search</button>
          {% if q or kind or group or month %}
            <a href="/expenses" style="font-size:13px; color:var(--ink-soft);">Clear</a>
          {% endif %}
        </div>
      </div>
    </form>
  </div>

  <div class="sectionhead">
    <h2>Expenses</h2>
    <span class="counts">showing {{ rows|length }} of {{ total }}</span>
    <span class="chip">${{ '%.2f' | format(spend) }}</span>
    {% for name, amount in by_type %}
      <span class="chip chip-soft">{{ name }} ${{ '%.2f' | format(amount) }}</span>
    {% endfor %}
    {% if saved and saved != '0' %}
      <span class="savedflash">{% if saved == '1' %}Saved{% else %}Saved {{ saved }} changes{% endif %}</span>
    {% endif %}
  </div>

  {% if rows %}
    <form id="expsave" method="post" action="/expenses/save">
      <input type="hidden" name="q" value="{{ q }}">
      <input type="hidden" name="type" value="{{ kind }}">
      <input type="hidden" name="group" value="{{ group }}">
      <input type="hidden" name="month" value="{{ month }}">
    </form>

    <div class="savebar">
      <button class="btn btn-primary" form="expsave" type="submit">&#128190; Save changes</button>
      <span class="savestate" id="savestate">No unsaved changes</span>
    </div>

    <div class="tablewrap">
      <table>
        <thead>
          <tr>
            <th>Date</th><th>Group</th><th>Client</th><th>Type</th>
            <th>Amount</th><th>Paid by</th><th>Location</th><th>Receipt</th><th></th>
          </tr>
        </thead>
        <tbody>
          {% for bundle in groups %}
            {% for row in bundle.rows %}
              <tr>
                <td class="editcell">
                  <input class="cellin whenin" type="date" form="expsave" style="width:142px; min-width:0;"
                         name="exp_{{ row._row }}_Date" value="{{ row.Date }}" onchange="markDirty()">
                </td>
                <td class="editcell">
                  <input class="cellin" type="text" form="expsave" style="width:138px;"
                         name="exp_{{ row._row }}_Group-ID" value="{{ row['Group ID'] }}"
                         list="grouplist" oninput="markDirty()">
                </td>
                <td class="editcell">
                  <input class="cellin" type="text" form="expsave" style="width:132px;"
                         name="exp_{{ row._row }}_Client" value="{{ row.Client }}"
                         list="clientlist" oninput="markDirty()">
                </td>
                <td class="editcell">
                  <select class="cellin" form="expsave" name="exp_{{ row._row }}_Type" onchange="markDirty()">
                    {% for t in types %}
                      <option value="{{ t }}" {% if t == row.Type %}selected{% endif %}>{{ t }}</option>
                    {% endfor %}
                    {% if row.Type not in types %}<option value="{{ row.Type }}" selected>{{ row.Type }}</option>{% endif %}
                  </select>
                </td>
                <td class="editcell money">
                  <input class="cellin amountin" type="text" form="expsave" inputmode="decimal"
                         name="exp_{{ row._row }}_Amount" value="{{ row.Amount }}" oninput="markDirty()">
                </td>
                <td class="editcell">
                  <input class="cellin" type="text" form="expsave" style="width:104px;"
                         name="exp_{{ row._row }}_Paid-by" value="{{ row['Paid by'] }}"
                         list="payerlist" oninput="markDirty()">
                </td>
                <td class="editcell">
                  <input class="cellin" type="text" form="expsave" style="width:146px;"
                         name="exp_{{ row._row }}_Location" value="{{ row.Location }}" oninput="markDirty()">
                </td>
                <td class="receiptcell">
                  {% if row.Receipt %}
                    <a href="{{ url_for('static', filename='receipts/' + row.Receipt) }}"
                       target="_blank" title="{{ row.Receipt }}">
                      {% if row.Receipt.lower().endswith('.pdf') %}
                        <span class="receipticon">&#128196;</span>
                      {% else %}
                        <img class="receiptthumb"
                             src="{{ url_for('static', filename='receipts/' + row.Receipt) }}" alt="receipt">
                      {% endif %}
                    </a>
                  {% else %}
                    <form method="post" action="/expenses/receipt/{{ row._row }}"
                          enctype="multipart/form-data" class="receiptform">
                      <input type="hidden" name="group" value="{{ group }}">
                      <label class="receiptadd" title="Attach a receipt">
                        &#128206;
                        <input type="file" name="receipt" accept="image/*,.pdf"
                               capture="environment" onchange="this.form.submit()">
                      </label>
                    </form>
                  {% endif %}
                </td>
                <td>
                  <form method="post" action="/expenses/delete/{{ row._row }}"
                        onsubmit="return confirm('Delete this expense?');">
                    <input type="hidden" name="group" value="{{ group }}">
                    <button class="btn btn-icon btn-danger" type="submit" title="Delete">&#128465;</button>
                  </form>
                </td>
              </tr>
            {% endfor %}
            {% if bundle.id and bundle.rows|length > 1 %}
              <tr class="grouptotal">
                <td colspan="4">{{ bundle.id }} - {{ bundle.rows|length }} items</td>
                <td class="money">${{ '%.2f' | format(bundle.total) }}</td>
                <td colspan="4"></td>
              </tr>
            {% endif %}
          {% endfor %}
        </tbody>
      </table>
    </div>

    <p class="muted" style="font-size:12.5px; margin-top:14px;">
      <b>{{ workbook }}</b> &rarr; sheet <b>{{ sheet_name }}</b> &middot;
      receipts in <b>static/receipts/</b>.
      Edit any box above, then press <b>Save changes</b>.
    </p>
  {% else %}
    <div class="panel empty">
      <p>{% if q or kind or group or month %}Nothing matches that filter.{% else %}No expenses logged yet.{% endif %}</p>
      <p style="font-size:13px;">Fill in the form at the top to add the first one.</p>
    </div>
  {% endif %}

  <script>
    var dirty = false;
    function markDirty() {
      dirty = true;
      var s = document.getElementById('savestate');
      if (s) { s.textContent = 'Unsaved changes'; s.classList.add('dirty'); }
    }
    document.addEventListener('DOMContentLoaded', function () {
      document.querySelectorAll('[form=expsave]').forEach(function (el) {
        var mark = function () { el.classList.toggle('touched', el.value !== el.defaultValue); };
        el.addEventListener('input', mark);
        el.addEventListener('change', mark);
      });
      var form = document.getElementById('expsave');
      if (form) { form.addEventListener('submit', function () { dirty = false; }); }
      window.addEventListener('beforeunload', function (e) {
        if (dirty) { e.preventDefault(); e.returnValue = ''; }
      });
    });
  </script>
</div>
</body>
</html>
"""


_PAGE_CACHE = {}


def logo_exists():
    import app
    return app.logo_exists()


def expenses_template():
    """Built once, from app.py's shared STYLE and HEADER, so the nav never drifts."""
    if "page" not in _PAGE_CACHE:
        import app
        _PAGE_CACHE["page"] = (_PAGE_BODY
                               .replace("{STYLE}", app.STYLE)
                               .replace("{HEADER}", app.HEADER))
    return _PAGE_CACHE["page"]


@bp.route("/expenses")
def expenses_page():
    rows = read_expenses()

    q = request.args.get("q", "").strip()
    kind = request.args.get("type", "").strip()
    group = request.args.get("group", "").strip()
    month = request.args.get("month", "").strip()

    shown = rows
    if kind:
        shown = [r for r in shown if r["Type"] == kind]
    if group:
        shown = [r for r in shown if r["Group ID"] == group]
    if month:
        shown = [r for r in shown if r["Date"].startswith(month)]
    if q:
        needle = q.lower()
        shown = [r for r in shown
                 if any(needle in str(r.get(h, "")).lower() for h in EXPENSE_HEADERS)]

    months = sorted({r["Date"][:7] for r in rows if len(r["Date"]) >= 7}, reverse=True)
    by_type = {}
    for row in shown:
        by_type[row["Type"] or "(none)"] = by_type.get(row["Type"] or "(none)", 0) + row["_amount"]

    return render_template_string(
        expenses_template(),
        rows=shown,
        groups=expense_groups(shown),
        total=len(rows),
        spend=sum(r["_amount"] for r in shown),
        by_type=sorted(by_type.items(), key=lambda kv: -kv[1]),
        types=EXPENSE_TYPES,
        group_ids=sorted({r["Group ID"] for r in rows if r["Group ID"]}, reverse=True),
        clients=sorted({r["Client"] for r in rows if r["Client"]}),
        payers=sorted({r["Paid by"] for r in rows if r["Paid by"]}),
        months=months,
        q=q, kind=kind, group=group, month=month,
        suggested_group=next_group_id(rows),
        today=datetime.now().date().isoformat(),
        workbook=store.XLSX_FILE,
        sheet_name=EXPENSE_SHEET,
        saved=request.args.get("saved", ""),
        has_logo=logo_exists(),
        notify_status=notify.status_line(),
        tab="expenses",
    )


@bp.route("/expenses/add", methods=["POST"])
def expenses_add():
    form = request.form
    amount = (form.get("amount", "") or "").strip().replace("$", "").replace(",", "")
    try:
        amount = f"{float(amount):.2f}"
    except ValueError:
        amount = ""

    expense_id = datetime.now().strftime("E%Y%m%d%H%M%S")
    group_id = (form.get("group_id", "") or "").strip()
    receipt = save_receipt(request.files.get("receipt"), group_id, expense_id)

    add_expense({
        "Expense ID": expense_id,
        "Group ID": group_id,
        "Date": (form.get("date", "") or "").strip(),
        "Client": (form.get("client", "") or "").strip(),
        "Amount": amount,
        "Type": (form.get("type", "") or "").strip(),
        "Paid by": (form.get("paid_by", "") or "").strip(),
        "Location": (form.get("location", "") or "").strip(),
        "Receipt": receipt,
        "Added": datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    return redirect(url_for("expenses.expenses_page",
                            group=group_id if form.get("stay") else None,
                            saved="1"))


@bp.route("/expenses/save", methods=["POST"])
def expenses_save():
    """Write back every edited cell in the table in one go."""
    changed = 0
    edits = {}
    for field, posted in request.form.items():
        if not field.startswith("exp_"):
            continue
        try:
            _, row_text, key_index = field.split("_", 2)
            excel_row = int(row_text)
        except ValueError:
            continue
        header = key_index.replace("-", " ")
        if header in EXPENSE_HEADERS:
            edits.setdefault(excel_row, {})[header] = (posted or "").strip()

    current = {r["_row"]: r for r in read_expenses()}
    for excel_row, values in edits.items():
        row = current.get(excel_row)
        if not row:
            continue
        diff = {h: v for h, v in values.items() if v != row.get(h, "")}
        if diff:
            update_expense(excel_row, diff)
            changed += len(diff)

    return redirect(url_for("expenses.expenses_page", saved=str(changed),
                            q=request.form.get("q", "") or None,
                            type=request.form.get("type", "") or None,
                            group=request.form.get("group", "") or None,
                            month=request.form.get("month", "") or None))


@bp.route("/expenses/receipt/<int:excel_row>", methods=["POST"])
def expenses_receipt(excel_row):
    rows = {r["_row"]: r for r in read_expenses()}
    row = rows.get(excel_row)
    if row:
        name = save_receipt(request.files.get("receipt"),
                            row.get("Group ID", ""), row.get("Expense ID", str(excel_row)))
        if name:
            update_expense(excel_row, {"Receipt": name})
    return redirect(url_for("expenses.expenses_page", group=request.form.get("group", "") or None))


@bp.route("/expenses/delete/<int:excel_row>", methods=["POST"])
def expenses_delete(excel_row):
    delete_expense(excel_row)
    return redirect(url_for("expenses.expenses_page", group=request.form.get("group", "") or None))


