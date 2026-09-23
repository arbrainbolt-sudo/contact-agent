"""Shared spreadsheet storage. One results.xlsx, several worksheets."""

import os
import csv
import threading
from datetime import datetime, date

from openpyxl import Workbook, load_workbook

XLSX_FILE = "results.xlsx"
LEGACY_CSV = "results.csv"

CONTACTS_SHEET = "Contacts"
LATEST_NEWS_SHEET = "LatestNews"
SAVED_NEWS_SHEET = "SavedNews"

# Sheets the app manages itself. Anything else in the workbook is yours.
MANAGED_SHEETS = {CONTACTS_SHEET, LATEST_NEWS_SHEET, SAVED_NEWS_SHEET}

_lock = threading.Lock()


def _workbook():
    if os.path.exists(XLSX_FILE):
        return load_workbook(XLSX_FILE)
    wb = Workbook()
    wb.remove(wb.active)          # drop the empty default sheet
    return wb


def _save_workbook(wb):
    if not wb.sheetnames:         # openpyxl refuses to save a sheetless file
        wb.create_sheet("Sheet1")
    wb.save(XLSX_FILE)


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
    return str(v)


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
            ws.append([row.get(f, "") for f in fields])
        _save_workbook(wb)


def clear_sheet(name):
    with _lock:
        wb = _workbook()
        if name in wb.sheetnames:
            wb.remove(wb[name])
        _save_workbook(wb)


# ---------------------------------------------------------------- your own sheets

def list_sheets():
    """Every worksheet in the workbook, in tab order."""
    if not os.path.exists(XLSX_FILE):
        return []
    with _lock:
        return list(_workbook().sheetnames)


def custom_sheets():
    """Sheets you created by hand — anything the app doesn't manage."""
    return [s for s in list_sheets() if s not in MANAGED_SHEETS]


def read_sheet_auto(name, max_rows=5000):
    """Read any sheet using whatever column headers it happens to have.

    Returns (headers, rows). Row 1 is treated as the header row.
    """
    if not os.path.exists(XLSX_FILE):
        return [], []

    with _lock:
        wb = _workbook()
        if name not in wb.sheetnames:
            return [], []
        values = list(wb[name].values)

    if not values:
        return [], []

    headers = []
    seen = set()
    for i, h in enumerate(values[0]):
        label = str(h).strip() if h is not None else ""
        if not label:
            label = f"Column {i + 1}"
        base, n = label, 2
        while label in seen:            # duplicate headers would collide
            label = f"{base} ({n})"
            n += 1
        seen.add(label)
        headers.append(label)

    rows = []
    for raw in values[1:max_rows + 1]:
        if all(v is None or str(v).strip() == "" for v in raw):
            continue                    # skip blank rows
        row = {}
        for i, h in enumerate(headers):
            row[h] = _cell_text(raw[i]) if i < len(raw) else ""
        rows.append(row)

    return headers, rows