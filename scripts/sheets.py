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


def tab_id(svc, cfg):
    """Numeric id of our tab, or None if it does not exist yet."""
    meta = svc.spreadsheets().get(spreadsheetId=cfg["sheet_id"]).execute()
    for sh in meta["sheets"]:
        if sh["properties"]["title"] == SHEET_NAME:
            return sh["properties"]["sheetId"]
    return None


def sheet_id_of(svc, cfg):
    """Numeric id of our tab, creating the tab if it isn't there yet."""
    existing = tab_id(svc, cfg)
    if existing is not None:
        return existing
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


def _has_protection(svc, cfg):
    """Is our warning-protection already on the tab? addProtectedRange is additive,
    so without this every push would stack another identical range."""
    meta = svc.spreadsheets().get(spreadsheetId=cfg["sheet_id"]).execute()
    for sh in meta["sheets"]:
        if sh["properties"]["title"] == SHEET_NAME:
            return any(r.get("description", "").startswith("Written by the pipeline")
                       for r in sh.get("protectedRanges", []))
    return False


def push(svc, cfg, obs, species, image_base, verified_by_file):
    """Write machine columns. Human columns are read first and written back untouched."""
    tab = sheet_id_of(svc, cfg)
    protected = _has_protection(svc, cfg)
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

    # An update only overwrites the range it writes, so a survey that SHRINKS —
    # records removed, a stand-in batch retired — leaves the old rows sitting
    # underneath the new ones. They look exactly like live records to a steward,
    # who would then spend a walk verifying something that no longer exists. Clear
    # everything below the last row we just wrote.
    svc.spreadsheets().values().clear(
        spreadsheetId=cfg["sheet_id"],
        range=f"{SHEET_NAME}!A{len(rows) + 2}:{last}").execute()

    n = len(rows) + 1
    requests = [
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
    ]
    if not protected:
        # Warn (don't block) on edits to pipeline columns — they are overwritten on
        # the next push, so an edit there is silently lost work.
        requests.append({"addProtectedRange": {"protectedRange": {
            "range": {"sheetId": tab, "startColumnIndex": 0, "endColumnIndex": FIRST_HUMAN},
            "description": "Written by the pipeline — edits here are overwritten on the next push.",
            "warningOnly": True}}})
    # .execute() is what actually sends it; a googleapiclient request object is lazy
    # and silently does nothing without it.
    svc.spreadsheets().batchUpdate(
        spreadsheetId=cfg["sheet_id"], body={"requests": requests}).execute()
    return len(rows)


def check_header(row):
    """Refuse to read a sheet whose columns have moved.

    Every read below is POSITIONAL — status is column 9 because that is where push
    put it. Insert, delete or reorder a column and each field silently becomes the
    one beside it: a steward's name read as a species id, field notes read as a
    date, or — the bad one — an emptied STATUS column read as "this verification
    was withdrawn" for every row at once, applied unattended by the hourly pull.

    None of the value checks downstream can catch that, because each individual
    value still looks plausible in its new position. The header is the only thing
    that knows the layout is wrong, so it is checked before anything is believed.
    """
    got = [(c or "").strip() for c in (list(row) + [""] * len(HEADERS))[:len(HEADERS)]]
    if got == HEADERS:
        return
    lines = ["The sheet's columns are not where the pipeline put them, so nothing "
             "can be read from it safely.\n"]
    for i, (want, have) in enumerate(zip(HEADERS, got)):
        mark = "  " if want == have else "->"
        lines.append(f"  {mark} {_col(i)}: expected {want!r}, found {have!r}")
    lines += [
        "",
        "Every field is read by position, so a shifted column would be read as the",
        "one next to it and quietly written into the survey. Fix it one of two ways:",
        "",
        "  * Undo the column change in the sheet (Google Sheets keeps a full history:",
        "    File -> Version history). Verifications are preserved.",
        "  * If the human columns are already lost, delete the 'Records' tab and run",
        "    `plantdb.py sheet-push` — it rebuilds the tab from scratch. Any",
        "    verification not already pulled into data/observations.json is gone.",
    ]
    sys.exit("\n".join(lines))


def pull(svc, cfg):
    """Read the human columns back. Returns {file: {status, species_id, by, date, notes}}.

    An absent tab means nothing has been pushed yet — that is empty, not an error.
    Checked explicitly rather than by catching HttpError, so a real failure (bad id,
    sheet not shared with the service account) still surfaces instead of looking
    like an empty sheet.
    """
    if tab_id(svc, cfg) is None:
        return {}
    last = _col(len(HEADERS) - 1)
    # From row 1, not row 2: the header is the only evidence that the columns still
    # mean what the positional reads below assume.
    res = svc.spreadsheets().values().get(
        spreadsheetId=cfg["sheet_id"], range=f"{SHEET_NAME}!A1:{last}").execute()
    values = res.get("values", [])
    if not values:
        return {}
    check_header(values[0])
    out = {}
    for row in values[1:]:
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
