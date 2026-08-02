#!/usr/bin/env python3
"""Two-way bridge between the survey and a Google Sheet the stewards can edit.

This is NOT a general two-way sync, and that is the whole point. Every column has
exactly one writer:

  * The pipeline owns the machine columns (file, photo, date, coordinates, the AI's
    identification). Push overwrites them; nothing read back from the sheet touches
    them.
  * A person owns the verification columns (status, corrected species, who checked
    it, when, field notes). Pull reads them; the pipeline never writes them.

Because no field has two writers, there is no merge and no conflict resolution to
get wrong. A steward can be editing the sheet while a batch run is identifying new
photos, and neither can clobber the other.

Setup lives in the README; credentials come from the environment:

    GOOGLE_SERVICE_ACCOUNT_JSON=/path/to/service-account.json
    GOOGLE_SHEET_ID=<the long id from the sheet's URL>
"""
import json, os, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SHEET_NAME = "Records"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# (header, owner). Order defines the sheet layout; changing it reorders the sheet.
COLUMNS = [
    ("file",             "pipeline"),
    ("photo",            "pipeline"),
    ("photographed",     "pipeline"),
    ("latitude",         "pipeline"),
    ("longitude",        "pipeline"),
    ("AI identification", "pipeline"),
    ("AI confidence",    "pipeline"),
    ("AI notes",         "pipeline"),
    ("STATUS",           "human"),
    ("corrected species", "human"),
    ("verified by",      "human"),
    ("verified date",    "human"),
    ("field notes",      "human"),
]
HEADERS = [c[0] for c in COLUMNS]
FIRST_HUMAN = next(i for i, (_, o) in enumerate(COLUMNS) if o == "human")
STATUSES = ["confirmed", "corrected", "rejected", "revisit"]


def config():
    """Credentials, or None if the sheet isn't set up. Never raises on absence."""
    key = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    sid = os.environ.get("GOOGLE_SHEET_ID", "").strip()
    if not key or not sid:
        return None
    return {"key": key, "sheet_id": sid}


def missing_vars():
    return [k for k in ("GOOGLE_SERVICE_ACCOUNT_JSON", "GOOGLE_SHEET_ID")
            if not os.environ.get(k, "").strip()]


def service(cfg):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    if not pathlib.Path(cfg["key"]).exists():
        sys.exit(f"Service account key not found: {cfg['key']}")
    creds = service_account.Credentials.from_service_account_file(cfg["key"], scopes=SCOPES)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _col(i):
    """0-based index to an A1 column letter."""
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def sheet_id_of(svc, cfg):
    """Numeric id of our tab, creating the tab if it isn't there yet."""
    meta = svc.spreadsheets().get(spreadsheetId=cfg["sheet_id"]).execute()
    for sh in meta["sheets"]:
        if sh["properties"]["title"] == SHEET_NAME:
            return sh["properties"]["sheetId"]
    res = svc.spreadsheets().batchUpdate(
        spreadsheetId=cfg["sheet_id"],
        body={"requests": [{"addSheet": {"properties": {"title": SHEET_NAME}}}]}).execute()
    return res["replies"][0]["addSheet"]["properties"]["sheetId"]


def row_for(o, species, image_base):
    """One record as a sheet row. Human columns are left empty — push never writes them."""
    sp = species.get(o.get("species_id"), {})
    thumb = o.get("thumb") or f"thumbs/{o['file']}"
    url = f"{image_base.rstrip('/')}/{thumb.split('/', 1)[-1]}" if image_base else ""
    return [
        o["file"],
        f'=IMAGE("{url}")' if url else "",
        (o.get("taken") or "")[:10],
        o.get("lat", ""),
        o.get("lon", ""),
        f"{sp.get('common', o.get('species_id',''))} ({sp.get('origin_status','?')})",
        o.get("confidence", ""),
        o.get("note", ""),
    ]


def push(svc, cfg, obs, species, image_base, verified_by_file):
    """Write machine columns. Human columns are read first and written back untouched."""
    tab = sheet_id_of(svc, cfg)
    rows = []
    for o in obs:
        machine = row_for(o, species, image_base)
        v = verified_by_file.get(o["file"], {})
        rows.append(machine + [
            v.get("status", ""), v.get("species_id", ""),
            v.get("by", ""), v.get("date", ""), v.get("notes", ""),
        ])

    last = _col(len(HEADERS) - 1)
    svc.spreadsheets().values().update(
        spreadsheetId=cfg["sheet_id"],
        range=f"{SHEET_NAME}!A1:{last}{len(rows) + 1}",
        valueInputOption="USER_ENTERED",
        body={"values": [HEADERS] + rows}).execute()

    n = len(rows) + 1
    svc.spreadsheets().batchUpdate(spreadsheetId=cfg["sheet_id"], body={"requests": [
        # Freeze the header, and the file column so a wide sheet stays readable.
        {"updateSheetProperties": {
            "properties": {"sheetId": tab, "gridProperties":
                           {"frozenRowCount": 1, "frozenColumnCount": 1}},
            "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount"}},
        {"repeatCell": {
            "range": {"sheetId": tab, "startRowIndex": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
            "fields": "userEnteredFormat.textFormat.bold"}},
        # Rows tall enough that the thumbnail is actually reviewable.
        {"updateDimensionProperties": {
            "range": {"sheetId": tab, "dimension": "ROWS", "startIndex": 1, "endIndex": n},
            "properties": {"pixelSize": 90}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {
            "range": {"sheetId": tab, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 2},
            "properties": {"pixelSize": 130}, "fields": "pixelSize"}},
        # A dropdown on STATUS: typos here would silently drop a verification.
        {"setDataValidation": {
            "range": {"sheetId": tab, "startRowIndex": 1, "endRowIndex": n,
                      "startColumnIndex": FIRST_HUMAN, "endColumnIndex": FIRST_HUMAN + 1},
            "rule": {"condition": {"type": "ONE_OF_LIST",
                                   "values": [{"userEnteredValue": s} for s in STATUSES]},
                     "showCustomUi": True, "strict": False}}},
        # Warn (don't block) on edits to pipeline columns — they are overwritten on
        # the next push, so an edit there is silently lost work.
        {"addProtectedRange": {"protectedRange": {
            "range": {"sheetId": tab, "startColumnIndex": 0, "endColumnIndex": FIRST_HUMAN},
            "description": "Written by the pipeline — edits here are overwritten on the next push.",
            "warningOnly": True}}},
    ]})
    return len(rows)


def pull(svc, cfg):
    """Read the human columns back. Returns {file: {status, species_id, by, date, notes}}."""
    last = _col(len(HEADERS) - 1)
    res = svc.spreadsheets().values().get(
        spreadsheetId=cfg["sheet_id"], range=f"{SHEET_NAME}!A2:{last}").execute()
    out = {}
    for row in res.get("values", []):
        row = row + [""] * (len(HEADERS) - len(row))
        f = (row[0] or "").strip()
        if not f:
            continue
        status = (row[FIRST_HUMAN] or "").strip().lower()
        out[f] = {
            "status": status,
            "species_id": (row[FIRST_HUMAN + 1] or "").strip(),
            "by": (row[FIRST_HUMAN + 2] or "").strip(),
            "date": (row[FIRST_HUMAN + 3] or "").strip(),
            "notes": (row[FIRST_HUMAN + 4] or "").strip(),
        }
    return out
