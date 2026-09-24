cd ~/Documents/contact-agent && cat > store.py << 'PYEOF'
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


# ---------------------------------------------------------------- backup

def _backup():
    """Copy the workbook to the backup, overwriting the old one.

    Runs BEFORE every write, so the backup holds the last good state.
    Assumes the caller already holds _lock.
    """
    if not os.path.exists(XLSX_FILE):
        return
    try:
        shutil.copy2(XLSX_FILE, BACKUP_FILE)
    except Exception as e:
        print(f"[store] backup failed: {e}")


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


def restore_backup():
    """Put the backup back as results.xlsx. Returns True on success."""
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


# ---------------------------------------------------------------- workbook

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

    Browsers submit CRLF for newlines in a textarea. Excel stores a lone CR
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
                print(f"[store] imported {len(legacy)} rows from {LEGACY_CSV}")
                return legacy
        return []


def write_sheet(name, fields, rows):
    with _lock:
        wb = _workbook()
        if name in wb.sheetnames:
            wb.remove(wb[name])
        ws = wb.create_sheet(name)
        ws.append(fields)