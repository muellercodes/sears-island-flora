#!/usr/bin/env python3
"""Client for the contributor inbox — the queue between the site and the survey.

WHY THERE IS AN INBOX AT ALL

`data/observations.json` has exactly one writer: the pipeline. That is the whole
reason this project has never had a merge conflict, and it is not something to
give up in order to let a steward tap a button on their phone.

So contributor mode does not write to the survey. It writes HERE, and the
pipeline drains it on the schedule it already runs on — which is precisely the
shape the Google Sheet already has. A sheet is an inbox a person edits by hand; a
Worker is an inbox a phone edits over HTTPS. Neither is a second writer, and both
are validated by the same functions in plantdb.py before anything is applied.

WHY THE DRAIN GOES THROUGH THE WORKER RATHER THAN STRAIGHT TO R2

The survey's R2 bucket is public — that is how the site serves thumbnails. An
inbox prefix in it would be world-readable to anyone with the key, and the inbox
holds full-resolution originals that still carry their EXIF, which the survey
strips from everything it publishes precisely so contributor camera serials do
not go out. So the bytes stay in a bucket bound to the Worker and never exposed,
and the pipeline collects them over an authenticated endpoint.

That also means the unattended run needs no R2 credential for any of this, and
the token it does hold is scoped to the `pipeline` role — which may collect the
inbox and nothing else. It cannot record a field check. Given that a field check
is the one thing this project asks anyone to believe, the credential that runs
every night without supervision should not be able to invent one.

Configuration (both gitignored in .env, both GitHub Actions secrets):

    SIF_WORKER_URL     https://sears-island-contributors.<subdomain>.workers.dev
    SIF_PIPELINE_TOKEN the drain token, minted by `plantdb.py contributor add --role pipeline`
"""
import json, os, urllib.error, urllib.request

TIMEOUT = 60


class InboxError(RuntimeError):
    pass


def config():
    """Worker URL and token, or None if contributor mode isn't set up.

    Absent is a normal state, not a failure: the survey worked without a Worker
    before and has to keep working without one, so every caller treats None as
    "there is no inbox" rather than as an error.
    """
    url = os.environ.get("SIF_WORKER_URL", "").strip().rstrip("/")
    token = os.environ.get("SIF_PIPELINE_TOKEN", "").strip()
    return {"url": url, "token": token} if url and token else None


def missing_vars():
    return [k for k in ("SIF_WORKER_URL", "SIF_PIPELINE_TOKEN")
            if not os.environ.get(k, "").strip()]


def _call(cfg, method, path, body=None, raw=False):
    data, ctype = None, None
    if body is not None:
        data, ctype = json.dumps(body).encode(), "application/json"
    req = urllib.request.Request(f"{cfg['url']}{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {cfg['token']}")
    if ctype:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            payload = r.read()
            return payload if raw else json.loads(payload or b"{}")
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode("utf-8", "replace")
        # 401/403 on the drain almost always means the token was rotated in one
        # place and not the other. Say so rather than making someone read a body.
        if e.code in (401, 403):
            raise InboxError(
                f"the Worker refused the pipeline token ({e.code}). Mint a new one with "
                "`plantdb.py contributor add --role pipeline` and update SIF_PIPELINE_TOKEN "
                "in .env and in the repository's Actions secrets.")
        raise InboxError(f"{method} {path}: HTTP {e.code} — {detail}")
    except urllib.error.URLError as e:
        raise InboxError(f"{method} {path}: {e.reason}")


def pending(cfg, limit=200):
    """Submissions waiting to be applied, oldest first.

    Oldest first matters: two marks on the same photograph, or a verification and
    then its withdrawal, have to be applied in the order the person made them.
    """
    got = _call(cfg, "GET", f"/api/inbox?limit={int(limit)}")
    return sorted(got.get("submissions", []), key=lambda s: s.get("submitted", ""))


def photo(cfg, sub_id):
    """The image bytes for an upload submission."""
    return _call(cfg, "GET", f"/api/inbox/{sub_id}/photo", raw=True)


def receipt(cfg, sub_id, status, detail=""):
    """Tell the Worker what became of a submission, and clear it from the queue.

    This is the inbox's version of the steward sheet's `recorded?` column, and it
    exists for the same reason: a refused submission that only appears in a CI log
    leaves the person who walked out there believing it was recorded. The site
    reads these back and shows each contributor what happened to theirs.

    `status` is "recorded" or "refused"; `detail` is the reason, in the words a
    contributor can act on.
    """
    return _call(cfg, "POST", f"/api/inbox/{sub_id}/receipt",
                 {"status": status, "detail": detail})


def check(cfg):
    """Confirm the Worker is reachable and the token is the drain token."""
    try:
        who = _call(cfg, "GET", "/api/me")
    except InboxError as e:
        return False, str(e)
    if who.get("role") != "pipeline":
        return False, (f"SIF_PIPELINE_TOKEN is a '{who.get('role')}' token, not a "
                       "'pipeline' one — the unattended run should hold the token that "
                       "can only collect the inbox")
    return True, f"reachable, draining as '{who.get('name', 'pipeline')}'"


if __name__ == "__main__":
    import sys
    cfg = config()
    if not cfg:
        sys.exit("Contributor mode is not configured. Missing: " + ", ".join(missing_vars()))
    ok, msg = check(cfg)
    print(("OK — " if ok else "FAILED — ") + msg)
    if ok:
        print(f"{len(pending(cfg))} submission(s) waiting.")
    sys.exit(0 if ok else 1)
