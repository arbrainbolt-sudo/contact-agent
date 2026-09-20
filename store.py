"""Shared spreadsheet storage. One results.xlsx, several worksheets."""

import os
import csv
import threading

from openpyxl import Workbook, load_workbook

XLSX_FILE = "results.xlsx"
LEGACY_CSV = "results.csv"

CONTACTS_SHEET = "Contacts"
LATEST_NEWS_SHEET = "LatestNews"
SAVED_NEWS_SHEET = "SavedNews"

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


def _rows_from_sheet(ws, fields):
    values = list(ws.values)
    if not values:
        return []
    header = [str(h) if h is not None else "" for h in values[0]]
    out = []
    for raw in values[1:]:
        record = {header[i]: ("" if v is None else str(v))
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