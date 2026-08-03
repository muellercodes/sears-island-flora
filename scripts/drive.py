#!/usr/bin/env python3
"""Pull contributor photos straight from a shared Google Drive folder.

The alternative is Google Drive for Desktop, which works but ties the pipeline to a
logged-in macOS session and a sync engine. This uses the service account already set
up for the review sheet: share one folder with it, and the watcher reads that folder
and nothing else. No desktop app, no local mirror, and it runs headlessly — which
matters the day this moves off a laptop.

Downloaded Drive file ids are remembered, so a folder is never re-downloaded. That is
a separate guarantee from the content-hash dedupe in ingest: the hash stops the same
*photo* being added twice, this stops us spending bandwidth fetching bytes we have
already seen.

    GOOGLE_SERVICE_ACCOUNT_JSON=/path/to/service-account.json   (shared with Sheets)
    GOOGLE_DRIVE_FOLDER_ID=<folder id from the Drive URL>
"""
import io, os, pathlib, sys

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
IMAGE_MIMES = ("image/jpeg", "image/png", "image/heic", "image/heif",
               "image/tiff", "image/webp")
FOLDER_MIME = "application/vnd.google-apps.folder"


def config():
    key = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    fid = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "").strip()
    if not key:
        return None
    return {"key": key, "folder_id": fid}


def missing_vars():
    return [k for k in ("GOOGLE_SERVICE_ACCOUNT_JSON", "GOOGLE_DRIVE_FOLDER_ID")
            if not os.environ.get(k, "").strip()]


def service(cfg):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    if not pathlib.Path(cfg["key"]).exists():
        sys.exit(f"Service account key not found: {cfg['key']}")
    creds = service_account.Credentials.from_service_account_file(cfg["key"], scopes=SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def explain(e):
    """Turn a Drive HttpError into something actionable, or re-raise it.

    The two failures anyone actually hits are 'API not switched on in the project'
    and 'folder never shared with the service account'. Both look like opaque 403/404
    tracebacks otherwise, and the second is easy to mistake for a wrong folder id.
    """
    from googleapiclient.errors import HttpError
    if not isinstance(e, HttpError):
        raise e
    status = getattr(e.resp, "status", None)
    body = str(e)
    if "accessNotConfigured" in body or "has not been used in project" in body:
        import re
        m = re.search(r"project[= ](\d+)", body)
        proj = m.group(1) if m else "<your project>"
        sys.exit("The Google Drive API is not enabled for this project.\n"
                 f"  Enable it: https://console.cloud.google.com/apis/library/drive.googleapis.com?project={proj}\n"
                 "  Then wait a minute for it to propagate and retry.\n"
                 "  (Sheets and Drive are separate APIs — enabling one does not enable the other.)")
    if status in (403, 404):
        sys.exit(f"Drive returned {status}. The usual cause is that the folder has not been\n"
                 "shared with the service account. Share it with the `client_email` from\n"
                 "your service-account JSON, then: plantdb.py drive-folders")
    raise e


def shared_folders(svc):
    """Every folder shared with the service account — how you find the folder id."""
    out, page = [], None
    while True:
        try:
            r = svc.files().list(
                q=f"mimeType='{FOLDER_MIME}' and trashed=false",
                fields="nextPageToken, files(id, name, owners(emailAddress))",
                pageSize=100, pageToken=page,
                includeItemsFromAllDrives=True, supportsAllDrives=True).execute()
        except Exception as e:
            explain(e)
        out.extend(r.get("files", []))
        page = r.get("nextPageToken")
        if not page:
            return out


def list_images(svc, folder_id):
    """Image files directly in the folder, newest first. Subfolders are not walked —
    a flat drop-box is easier for contributors to get right than a hierarchy."""
    mimes = " or ".join(f"mimeType='{m}'" for m in IMAGE_MIMES)
    out, page = [], None
    while True:
        try:
            r = svc.files().list(
                q=f"'{folder_id}' in parents and trashed=false and ({mimes})",
                fields="nextPageToken, files(id, name, size, createdTime, md5Checksum)",
                orderBy="createdTime desc", pageSize=200, pageToken=page,
                includeItemsFromAllDrives=True, supportsAllDrives=True).execute()
        except Exception as e:
            explain(e)
        out.extend(r.get("files", []))
        page = r.get("nextPageToken")
        if not page:
            return out


def download(svc, file_id, dest):
    from googleapiclient.http import MediaIoBaseDownload
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = svc.files().get_media(fileId=file_id, supportsAllDrives=True)
    with io.FileIO(dest, "wb") as fh:
        dl = MediaIoBaseDownload(fh, req, chunksize=4 * 1024 * 1024)
        done = False
        while not done:
            _, done = dl.next_chunk()
    return dest
