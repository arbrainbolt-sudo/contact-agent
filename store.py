"""Shared spreadsheet storage. One results.xlsx, several worksheets."""

import os
import csv
import shutil
import threading
from datetime import datetime, date

from openpyxl import Workbook, load_workbook

XLSX_FILE = "results.xlsx"
BACKUP_FILE = "results_backup.xlsx"
LEGACY_CSV = "results.csv"

CONTACTS_SHEET = "Contacts"
LATEST_NEWS_SHEET = "LatestNews"
SAVED_NEWS_SHEET = "SavedNews"
CRM_SUGGESTIONS_SHEET = "CrmSuggestions"

# Sheets the app manages itself. Anything else in the workbook is yours.
MANAGED_SHEETS = {CONTACTS_SHEET, LATEST_NEWS_SHEET, SAVED_NEWS_SHEET, CRM_SUGGESTIONS_SHEET}

_lock = threading.Lock()


def _backup():
    """Copy the workbook to results_backup.xlsx, overwriting the old backup.

    Runs BEFORE every write, so the backup always holds the last good state.
    """
    if not os.path.exists(XLSX_FILE):
        return
    try:
        shutil.copy2(XLSX_FILE, BACKUP_FILE)
    except Exception as e:
        print(f"[store] backup failed: {e}")


def _workbook():
    if os.path.exists(XLSX_FILE):
        return load_workbook(XLSX_FILE)
    wb = Workbook()
    wb.remove(wb.active)          # drop the empty default sheet
    return wb


def _save_workbook(wb):
    if not wb.sheetnames:         # openpyxl refuses to save a sheetless file
        wb.create_sheet("Sheet1")
    _backup()                     # snapshot the good copy first
    wb.save(XLSX_FILE)


def _clean_text(value):
    """Normalise line endings and strip Excel's carriage-return escape.

    Browsers submit \\r\\n for newlines in a textarea. Excel stores a lone \\r
    as the literal text _x000D_, which then shows up in the cell.
    """
    text = "" if value is None else str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.replace("_x000D_", "")


def _cell_text(v):
    """Turn any Excel cell value into something printable."""
    if v is None:
        return ""
    if isinstance(v, datetime):
        if v.hour == 0 and v.minute == 0 and v.second == 0:
            return v.strftime("%Y-%m-%d")
        return v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, date):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, float) and v.is_integer():
        return str(int(v))          # 42.0 -> "42"
    return str(v).replace("_x000D_", "")


def _rows_from_sheet(ws, fields):
    values = list(ws.values)
    if not values:
        return []
    header = [str(h) if h is not None else "" for h in values[0]]
    out = []
    for raw in values[1:]:
        record = {header[i]: _cell_text(v)
                  for i, v in enumerate(raw) if i < len(header)}
        out.append({f: record.get(f, "") for f in fields})
    return out


def _import_legacy_csv(fields):
    """One-time pickup of the old results.csv."""
    if not os.path.exists(LEGACY_CSV):
        return []
    with open(LEGACY_CSV, newline="", encoding="utf-8") as f:
        return [{k: (row.get(k) or "") for k in fields} for row in csv.DictReader(f)]


def read_sheet(name, fields):
    with _lock:
        wb = _workbook()
        if name in wb.sheetnames:
            return _rows_from_sheet(wb[name], fields)

        # first time: pull contacts across from the old CSV
        if name == CONTACTS_SHEET:
            legacy = _import_legacy_csv(fields)
            if legacy:
                ws = wb.create_sheet(name)
                ws.append(fields)
                for row in legacy:
                    ws.append([row.get(f, "") for f in fields])
                _save_workbook(wb)
                print(f"[store] imported {len(legacy)} rows from {LEGACY_CSV} "
                      f"into {XLSX_FILE} / {name}")
                return legacy
        return []


def write_sheet(name, fields, rows):
    with _lock:
        wb = _workbook()
        if name in wb.sheetnames:
            wb.remove(wb[name])
        ws = wb.create_sheet(name)
        ws.append(fields)
        for row in rows:
            ws.append([_clean_text(row.get(f, "")) for f in fields])
        _save_workbook(wb)


def clear_sheet(name):
    with _lock:
        wb = _workbook()
        if name in wb.sheetnames:
            wb.remove(wb[name])
        _save_workbook(wb)


def restore_backup():
    """Put results_backup.xlsx back as results.xlsx. Returns True on success."""
    if not os.path.exists(BACKUP_FILE):
        return False
    with _lock:
        try:
            shutil.copy2(BACKUP_FILE, XLSX_FILE)
            print(f"[store] restored {XLSX_FILE} from {BACKUP_FILE}")
            return True
        except Exception as e:
            print(f"[store] restore failed: {e}")
            return False


def backup_info():
    """When the backup was last written, and how big it is."""
    if not os.path.exists(BACKUP_FILE):
        return {"exists": False, "when": "", "size": 0}
    stat = os.stat(BACKUP_FILE)
    return {
        "exists": True,
        "when": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        "size": stat.st_size,
    }


# ---------------------------------------------------------------- your own sheets

def list_sheets():
    """Every worksheet in the workbook, in tab order."""
    if not os.path.exists(XLSX_FILE):
        return []
    with _lock:
        return list(_workbook().sheetnames)


def custom_sheets():
    """Sheets you created by hand - anything the app doesn't manage."""
    return [s for s in list_sheets() if s not in MANAGED_SHEETS]


def _header_labels(ws):
    """Row 1 as display labels, blanks and duplicates made unique."""
    labels = []
    seen = set()
    for i, cell in enumerate(ws[1] if ws.max_row >= 1 else []):
        label = str(cell.value).strip() if cell.value is not None else ""
        if not label:
            label = f"Column {i + 1}"
        base, n = label, 2
        while label in seen:
            label = f"{base} ({n})"
            n += 1
        seen.add(label)
        labels.append(label)
    return labels


def read_sheet_auto(name, max_rows=5000):
    """Read any sheet using whatever column headers it happens to have.

    Returns (headers, rows). Each row carries "_row" - its real Excel row
    number - so edits can be written back to the right place.
    """
    if not os.path.exists(XLSX_FILE):
        return [], []

    with _lock:
        wb = _workbook()
        if name not in wb.sheetnames:
            return [], []
        ws = wb[name]
        if ws.max_row < 1:
            return [], []
        headers = _header_labels(ws)
        raw_rows = [(r[0].row, [c.value for c in r])
                    for r in ws.iter_rows(min_row=2, max_row=min(ws.max_row, max_rows + 1))]

    if not headers:
        return [], []

    rows = []
    for excel_row, raw in raw_rows:
        if all(v is None or str(v).strip() == "" for v in raw):
            continue                    # skip blank rows
        row = {"_row": excel_row}
        for i, h in enumerate(headers):
            row[h] = _cell_text(raw[i]) if i < len(raw) else ""
        rows.append(row)

    return headers, rows


def update_cell(name, excel_row, col_number, value):
    """Write one cell by its real Excel coordinates. Returns True on success."""
    value = _clean_text(value)
    with _lock:
        wb = _workbook()
        if name not in wb.sheetnames:
            return False
        ws = wb[name]
        if excel_row < 2 or excel_row > ws.max_row:
            return False
        if col_number < 1 or col_number > max(ws.max_column, 1):
            return False
        ws.cell(row=excel_row, column=col_number, value=value)
        _save_workbook(wb)
        return True


def append_blank_row(name, values_by_col):
    """Add a row at the bottom. values_by_col maps column number -> text."""
    values_by_col = {k: _clean_text(v) for k, v in values_by_col.items()}
    with _lock:
        wb = _workbook()
        if name not in wb.sheetnames:
            return 0
        ws = wb[name]
        new_row = ws.max_row + 1
        width = max(ws.max_column, max(values_by_col.keys(), default=1))
        for col in range(1, width + 1):
            ws.cell(row=new_row, column=col, value=values_by_col.get(col, ""))
        _save_workbook(wb)
        return new_row


    def backup_now():
    """Manual backup, triggered from the UI."""
    with _lock:
        if not os.path.exists(XLSX_FILE):
            print("[store] nothing to back up")
            return False
        try:
            shutil.copy2(XLSX_FILE, BACKUP_FILE)
            print(f"[store] backed up {XLSX_FILE} -> {BACKUP_FILE}")
            return True
        except Exception as e:
            print(f"[store] backup failed: {e}")
            return False