#!/usr/bin/env python3
"""Minimal Cloudflare R2 client — just enough to upload thumbnails.

R2 speaks the S3 API, so this signs requests with AWS SigV4 using nothing but the
standard library. The alternative, shelling out to `wrangler r2 object put`, spawns
a Node process per object; at survey scale (thousands of photos) that is hours of
process startup, so this issues plain HTTPS PUTs instead.

Credentials come from the environment (autopilot.sh already sources .env):

    R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET

The public URL base is NOT a secret and lives in data/publish-config.json, so the
deploy runner can build image URLs without ever holding credentials.
"""
import datetime, hashlib, hmac, os, sys, urllib.request, urllib.error

REGION, SERVICE = "auto", "s3"
UNSIGNED = "UNSIGNED-PAYLOAD"


class R2Error(RuntimeError):
    pass


def config():
    """Return credentials, or None if R2 isn't set up. Never raises on absence."""
    need = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")
    vals = {k: os.environ.get(k, "").strip() for k in need}
    if not all(vals.values()):
        return None
    return vals


def missing_vars():
    need = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")
    return [k for k in need if not os.environ.get(k, "").strip()]


def _sign(key, msg):
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _signing_key(secret, datestamp):
    k = _sign(("AWS4" + secret).encode(), datestamp)
    k = _sign(k, REGION)
    k = _sign(k, SERVICE)
    return _sign(k, "aws4_request")


def _quote(s):
    """S3 canonical URI encoding: everything except unreserved chars, and '/' kept."""
    safe = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~/"
    return "".join(c if c in safe else "".join(f"%{b:02X}" for b in c.encode()) for c in s)


def signed_request(cfg, method, key, body=b"", content_type=None, payload_hash=None):
    """Build a signed urllib Request for <bucket>/<key>."""
    host = f"{cfg['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com"
    path = _quote(f"/{cfg['R2_BUCKET']}/{key}")
    now = datetime.datetime.now(datetime.timezone.utc)
    amzdate = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    ph = payload_hash or hashlib.sha256(body).hexdigest()

    headers = {"host": host, "x-amz-content-sha256": ph, "x-amz-date": amzdate}
    if content_type:
        headers["content-type"] = content_type
    signed_headers = ";".join(sorted(headers))
    canon_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
    canon = f"{method}\n{path}\n\n{canon_headers}\n{signed_headers}\n{ph}"

    scope = f"{datestamp}/{REGION}/{SERVICE}/aws4_request"
    to_sign = ("AWS4-HMAC-SHA256\n" + amzdate + "\n" + scope + "\n"
               + hashlib.sha256(canon.encode()).hexdigest())
    sig = hmac.new(_signing_key(cfg["R2_SECRET_ACCESS_KEY"], datestamp),
                   to_sign.encode(), hashlib.sha256).hexdigest()

    headers["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={cfg['R2_ACCESS_KEY_ID']}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={sig}")
    req = urllib.request.Request(f"https://{host}{path}", data=body or None, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    return req


def put(cfg, key, data, content_type="image/jpeg", retries=3):
    """Upload one object. Retries transient failures; raises R2Error otherwise."""
    last = None
    for attempt in range(1, retries + 1):
        req = signed_request(cfg, "PUT", key, data, content_type)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                if 200 <= r.status < 300:
                    return True
                last = f"HTTP {r.status}"
        except urllib.error.HTTPError as e:
            body = e.read()[:300].decode("utf-8", "replace")
            # 4xx other than throttling means the request is wrong; retrying won't help.
            if e.code not in (429, 500, 502, 503, 504):
                raise R2Error(f"{key}: HTTP {e.code} — {body}")
            last = f"HTTP {e.code}"
        except Exception as e:
            last = str(e)
        if attempt < retries:
            import time
            time.sleep(2 ** attempt)
    raise R2Error(f"{key}: giving up after {retries} attempts — {last}")


def check(cfg):
    """Verify credentials work before a run uploads hundreds of objects."""
    key = ".plantdb-preflight"
    try:
        put(cfg, key, b"ok", "text/plain", retries=1)
        return True, "credentials OK"
    except R2Error as e:
        return False, str(e)


if __name__ == "__main__":
    cfg = config()
    if not cfg:
        sys.exit("R2 is not configured. Missing: " + ", ".join(missing_vars()))
    ok, msg = check(cfg)
    print(("OK — " if ok else "FAILED — ") + msg)
    sys.exit(0 if ok else 1)
