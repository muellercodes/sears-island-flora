#!/usr/bin/env python3
"""
Identify unidentified photos with the Claude API.

  .venv/bin/python scripts/identify.py            # identify everything awaiting ID
  .venv/bin/python scripts/identify.py --limit 5  # try a few first
  .venv/bin/python scripts/identify.py --all-unknown   # also retry the hand-flagged unknowns

Needs an Anthropic API key:  export ANTHROPIC_API_KEY=sk-ant-...

Everything it writes goes straight into the guide and is marked as AI-identified,
which the site states prominently at the top of every page.
"""
import argparse, base64, json, os, pathlib, sys, datetime

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SPECIES_F, OBS_F = DATA / "species.json", DATA / "observations.json"
THUMBS = ROOT / "thumbs"

MODEL = "claude-opus-5"
EFFORT = "medium"
MAX_TOKENS = 8000  # caps thinking + response together on Opus 5

DEFAULT_REGION = "Sears Island, Searsport, Maine — a coastal island in Penobscot Bay"

SYSTEM = """\
You process photographs submitted by volunteers for a botanical survey of \
{region}. The survey's purpose is to inventory the flora and locate non-native \
and regulated invasive species.

FIRST, screen the image. Set `is_survey_photo` false for anything that is not a \
photograph of plants, fungi, or vegetated landscape — including photographs \
containing people as a subject, screenshots, documents, indoor scenes, animals, \
or anything else off-topic. State why in `rejection_reason`. Judge what the image \
IS, not whether it is objectionable: an allowlist is more reliable here than trying \
to enumerate what to exclude. If a person appears incidentally in the background of \
an otherwise valid vegetation photo, accept it but say so in `rejection_reason`. \
When you reject, leave every other field at its empty value.

THEN, if it passed, identify the main plant or fungus.

Two rules govern the identification:

1. Be honest about uncertainty. This survey may inform land-management decisions \
on contested ground, so a confident wrong answer is worse than no answer. If the \
photo does not show enough to identify the organism — no flowers, no fruit, a \
habitat shot, a mushroom from above only — say so, return kind "other", and set \
confidence "low". Identify only to the rank the photo supports: genus is a fine \
answer when species is not visible.

2. Flag anything that could be a regulated invasive, even at low confidence. A \
false positive costs someone a walk to go check; a false negative misses an \
infestation while it is still small. When a specimen resembles something on the \
watchlist, say so in `note` and name the feature that would settle it.

Prefer reusing an existing species entry over inventing a near-duplicate: if the \
organism matches one of the existing entries you are given, return its id in \
`matches_existing_id` and leave the descriptive fields empty.

Write for someone standing in front of the plant. ID marks should be things they \
can check right now — leaf arrangement, stem shape, smell, what happens when you \
tear a leaf — not range maps or microscopy. Cautions should name the specific \
harm. Lookalikes should name what to rule out and the single feature that \
separates them.

If other species from the catalogue are clearly visible in the photo but are not \
its main subject, list their ids in `also_visible`. A plant caught in the \
background is still a real record of it growing at that spot."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["is_survey_photo", "rejection_reason", "origin_status",
                 "matches_existing_id", "confidence", "note", "common", "scientific",
                 "family", "kind", "edibility", "danger", "summary", "parts",
                 "id_marks", "cautions", "lookalikes", "also_visible"],
    "properties": {
        "is_survey_photo": {
            "type": "boolean",
            "description": "true only if this is a photograph of plants, fungi, or vegetated landscape suitable for a botanical survey",
        },
        "rejection_reason": {
            "type": "string",
            "description": "If is_survey_photo is false, why. Also used to note an incidental person in the background of an otherwise valid photo. Empty string otherwise.",
        },
        "origin_status": {
            "type": "string",
            "enum": ["native", "introduced", "invasive", "regulated", "unknown"],
            "description": "Status in Maine. 'regulated' = on the state Do Not Sell list. 'unknown' when identified only to genus and the genus contains both native and introduced species.",
        },
        "matches_existing_id": {
            "type": ["string", "null"],
            "description": "id of an existing species entry this photo shows, or null if it is a species not yet in the guide",
        },
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "note": {
            "type": "string",
            "description": "One or two sentences on what is actually visible in THIS photo and what you keyed on. If you could not identify it, say what is missing.",
        },
        "common": {"type": "string", "description": "Common name. Empty string if matches_existing_id is set."},
        "scientific": {"type": "string", "description": "Scientific name, genus at minimum. Use 'cf.' or 'sp.' when unsure. Empty string if matches_existing_id is set."},
        "family": {"type": "string"},
        "kind": {"type": "string", "enum": ["herb", "shrub", "tree", "vine", "fern", "fungus", "grass", "moss", "other"]},
        "edibility": {
            "type": "string",
            "enum": ["edible", "edible-with-caution", "medicinal-only", "inedible", "toxic", "unknown"],
        },
        "danger": {"type": "boolean", "description": "true if touching or eating this could cause real harm"},
        "summary": {"type": "string", "description": "Two or three sentences: what this is and why it matters to a forager."},
        "parts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["part", "use", "season", "prep"],
                "properties": {
                    "part": {"type": "string", "description": "e.g. 'Young leaves', 'Root', 'Ripe berries'"},
                    "use": {"type": "string", "description": "e.g. 'Food', 'Tea', 'Medicinal', 'Dye', 'Utility'"},
                    "season": {"type": "string", "description": "When to harvest, e.g. 'After first frost'"},
                    "prep": {"type": "string", "description": "How to prepare it, including any required processing"},
                },
            },
        },
        "id_marks": {"type": "array", "items": {"type": "string"}},
        "cautions": {"type": "array", "items": {"type": "string"}},
        "lookalikes": {"type": "array", "items": {"type": "string"}},
        "also_visible": {
            "type": "array",
            "items": {"type": "string"},
            "description": "ids of OTHER existing species from the catalogue that are clearly visible in this photo but are not its main subject. Only ids you are confident about; empty list if none.",
        },
    },
}


def load(p):
    return json.load(open(p))


def save(p, o):
    json.dump(o, open(p, "w"), indent=1)


# Observations are split across a tracked file and a gitignored local-only one.
# Reuse plantdb's split so an identification written here lands back in the file
# the record came from — a local-only photo must never be promoted into git.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from plantdb import load_obs, save_obs, thumb_path, require  # noqa: E402
import idcache  # noqa: E402


def slugify(name, taken):
    s = "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-")
    while "--" in s:
        s = s.replace("--", "-")
    s = s[:40] or "unnamed"
    base, n = s, 2
    while s in taken:
        s, n = f"{base}-{n}", n + 1
    return s


def describe(obs):
    bits = []
    if obs.get("taken"):
        d = obs["taken"][:10]
        try:
            month = datetime.date.fromisoformat(d).strftime("%B")
            bits.append(f"Photographed {d} ({month})")
        except ValueError:
            bits.append(f"Photographed {d}")
    if obs.get("lat"):
        bits.append(f"at {obs['lat']}, {obs['lon']}")
    if obs.get("batch"):
        bits.append(f"from the batch '{obs['batch']}'")
    return ". ".join(bits) + "." if bits else "No capture metadata available."


def apply_result(r, o, ctx, cached_sid=None):
    """Turn one model response into survey state. Shared by the sync and batch paths.

    Mutates the observation and, when a genuinely new species is found, the
    catalogue. Returns (outcome, label) where outcome is "done" | "rejected".
    Kept in one place so the two paths can never drift into disagreeing about what
    a result means.
    """
    species, by_id, args = ctx["species"], ctx["by_id"], ctx["args"]

    # Screening gate: anything that isn't a vegetation photo never enters the survey.
    if not r.get("is_survey_photo", True):
        reason = r.get("rejection_reason") or "not a vegetation photograph"
        o["species_id"] = "unknown"
        o["confidence"] = "rejected"
        o["note"] = f"Screened out: {reason}"
        o["rejected"] = True
        o["identified"] = datetime.date.today().isoformat()
        # Delete the thumbnail. thumbs/ is tracked in git — it's how the deploy
        # runner gets images — so leaving a screened-out photo there would commit
        # it to a public repo forever. The original stays in gitignored photos/.
        thumb_path(o).unlink(missing_ok=True)
        return "rejected", f"REJECTED — {reason[:60]}"

    # Fungi never get an edible verdict from a photo, whatever came back.
    if r.get("kind") == "fungus" and r.get("edibility") not in ("toxic", "unknown"):
        r["edibility"] = "unknown"
        r["cautions"] = ["Identified from a photograph only — never eat a wild mushroom on a photo ID."] \
            + (r.get("cautions") or [])

    if cached_sid and (cached_sid in by_id or cached_sid == "unknown"):
        # Replaying a cached result must resolve to the same species it did the
        # first time. Without this, a replay re-runs the creation path, finds the
        # slug taken, and mints a duplicate ("-2") of a species we already have.
        sid = cached_sid
        label = f"→ {by_id.get(sid, {}).get('common', sid)}"
    elif r.get("matches_existing_id") and r["matches_existing_id"] in by_id:
        sid = r["matches_existing_id"]
        label = f"→ {by_id[sid]['common']}"
    elif r.get("kind") == "other" and r.get("confidence") == "low":
        # A non-answer, not a species. The prompt asks for kind "other" at low
        # confidence when the photo cannot support an identification — a habitat
        # shot, a canopy, a seedling with no diagnostic features. Minting a
        # catalogue entry for that invents a species that does not exist, and the
        # catalogue is sent with every subsequent photo. Leave it unknown; the
        # note still records what was missing, which is the useful part.
        sid = "unknown"
        label = "unidentifiable from this photo"
    else:
        sid = slugify(r.get("common") or "unnamed", set(by_id))
        entry = {
            "id": sid,
            "common": r.get("common") or "Unidentified",
            "scientific": r.get("scientific") or "—",
            "family": r.get("family") or "—",
            "kind": r.get("kind", "other"),
            "edibility": r.get("edibility", "unknown"),
            "summary": r.get("summary", ""),
            "parts": r.get("parts") or [],
            "id_marks": r.get("id_marks") or [],
            "cautions": r.get("cautions") or [],
            "lookalikes": r.get("lookalikes") or [],
            "origin_status": r.get("origin_status", "unknown"),
            "source": f"auto ({args.model})",
        }
        if r.get("danger"):
            entry["danger"] = True
        species.append(entry)
        by_id[sid] = entry
        ctx["catalogue"] += f"\n- {sid}: {entry['common']} ({entry['scientific']})"
        flag = " ** REGULATED **" if entry["origin_status"] == "regulated" else (
               " * invasive *" if entry["origin_status"] == "invasive" else "")
        label = f"NEW {entry['common']} [{entry['origin_status']}]{flag}"

    o["species_id"] = sid
    o["confidence"] = r.get("confidence", "low")
    o["note"] = r.get("note", "")
    # When this record's identification was last written. The site shows it so a
    # reader can tell a fresh ID from one that has sat unrevised.
    o["identified"] = datetime.date.today().isoformat()
    also = [x for x in (r.get("also_visible") or []) if x in by_id and x != sid]
    if also:
        o["also"] = also
    return "done", label


# --- Batch API -------------------------------------------------------------
# Identification is not latency-sensitive: a survey does not care whether an answer
# lands in four seconds or four hours. The Batch API halves the price for exactly
# that trade. Requests are capped at 256 MB per batch and results come back in any
# order, so we chunk by real payload size and key every result by custom_id.

BATCH_MAX_BYTES = 200 * 1024 * 1024   # 256 MB hard limit; leave real headroom
BATCH_MAX_REQUESTS = 5000


def build_request(o, ctx):
    """One batch request. custom_id is the content hash — the photo's real identity."""
    args = ctx["args"]
    img = base64.standard_b64encode(thumb_path(o).read_bytes()).decode()
    return {
        "custom_id": o["hash"],
        "params": {
            "model": args.model,
            "max_tokens": MAX_TOKENS,
            "system": [{
                "type": "text",
                "text": SYSTEM.format(region=args.region)
                        + "\n\nSpecies already in this guide — reuse one of these ids if "
                          f"the photo shows the same organism:\n{ctx['catalogue']}",
                "cache_control": {"type": "ephemeral"},
            }],
            "output_config": {"effort": args.effort,
                              "format": {"type": "json_schema", "schema": SCHEMA}},
            "messages": [{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img}},
                {"type": "text", "text": f"{describe(o)}\n\nIdentify the main plant or fungus in this photograph."},
            ]}],
        },
    }


def chunk_requests(reqs):
    """Split on the 256 MB ceiling. A base64 thumbnail is ~270 KB, so this bites."""
    out, cur, size = [], [], 0
    for r in reqs:
        n = len(json.dumps(r))
        if cur and (size + n > BATCH_MAX_BYTES or len(cur) >= BATCH_MAX_REQUESTS):
            out.append(cur); cur, size = [], 0
        cur.append(r); size += n
    if cur:
        out.append(cur)
    return out


def submit_batches(client, pending, ctx, cache):
    args = ctx["args"]
    reqs = [build_request(o, ctx) for o in pending if o.get("hash")]
    chunks = chunk_requests(reqs)
    ids = []
    for n, chunk in enumerate(chunks, 1):
        b = client.messages.batches.create(requests=chunk)
        idcache.record_batch(cache, b.id, len(chunk), args.model, args.region)
        ids.append(b.id)
        print(f"  submitted batch {n}/{len(chunks)}: {b.id}  ({len(chunk)} photos)")
    return ids


def collect_batch(client, batch_id, obs, ctx, cache, counters):
    """Apply one finished batch. Results arrive in any order — key by custom_id."""
    by_hash = {o["hash"]: o for o in obs if o.get("hash")}
    for res in client.messages.batches.results(batch_id):
        o = by_hash.get(res.custom_id)
        if o is None:
            print(f"  ! result for an unknown photo ({res.custom_id}) — skipped")
            continue
        kind = res.result.type
        if kind != "succeeded":
            err = getattr(getattr(res.result, "error", None), "type", kind)
            print(f"  ! {o['file']}: {kind} ({err})")
            counters["failed"] += 1
            continue
        msg = res.result.message
        if msg.stop_reason in ("refusal", "max_tokens"):
            print(f"  ! {o['file']}: {msg.stop_reason}")
            counters["failed"] += 1
            continue
        text = next((b.text for b in msg.content if b.type == "text"), None)
        if not text:
            counters["failed"] += 1
            continue
        try:
            r = json.loads(text)
        except json.JSONDecodeError:
            print(f"  ! {o['file']}: unparseable response")
            counters["failed"] += 1
            continue
        outcome, label = apply_result(r, o, ctx)
        idcache.put(cache, o["hash"], o["file"], r, ctx["args"].model, ctx["args"].region,
                    msg.usage, species_id=o["species_id"])
        u = msg.usage
        counters["in"] += u.input_tokens or 0
        counters["out"] += u.output_tokens or 0
        counters[outcome] += 1
        print(f"  {o['file']}: {label}")
    idcache.mark_collected(cache, batch_id)


def wait_for(client, batch_ids, poll=30):
    """Block until every batch has ended, reporting progress as it goes."""
    import time
    remaining = list(batch_ids)
    while remaining:
        still = []
        for bid in remaining:
            b = client.messages.batches.retrieve(bid)
            if b.processing_status == "ended":
                c = b.request_counts
                print(f"  {bid}: ended  ({c.succeeded} ok, {c.errored} errored, {c.expired} expired)")
            else:
                still.append(bid)
                c = b.request_counts
                print(f"  {bid}: {b.processing_status}  ({c.processing} processing, {c.succeeded} done)")
        remaining = still
        if remaining:
            time.sleep(poll)
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, help="only do this many (good for a first test)")
    ap.add_argument("--all-unknown", action="store_true",
                    help="also retry photos a human already looked at and left unknown")
    ap.add_argument("--max-attempts", type=int, default=2,
                    help="stop retrying a photo after this many identification attempts (0 = no cap)")
    ap.add_argument("--retry-exhausted", action="store_true",
                    help="ignore --max-attempts and retry photos that have hit the cap")
    ap.add_argument("--batch", action="store_true",
                    help="use the Batch API: half price, results within hours not seconds")
    ap.add_argument("--no-wait", action="store_true",
                    help="with --batch, submit and exit; collect later with --collect")
    ap.add_argument("--collect", action="store_true",
                    help="apply results from batches already submitted")
    ap.add_argument("--ignore-cache", action="store_true",
                    help="re-identify even photos already in the cache (costs money again)")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--effort", default=EFFORT, choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--region", default=os.environ.get("PLANT_REGION", DEFAULT_REGION),
                    help=f"where these photos were taken, e.g. 'the Pacific Northwest' (default: {DEFAULT_REGION})")
    args = ap.parse_args()

    anthropic = require("anthropic", "anthropic")

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        sys.exit("No API key found. Get one at https://console.anthropic.com/settings/keys then:\n"
                 "  export ANTHROPIC_API_KEY=sk-ant-...")

    client = anthropic.Anthropic()
    species, obs = load(SPECIES_F), load_obs()
    by_id = {s["id"]: s for s in species}

    pending = [o for o in obs if o["species_id"] == "unknown"
               and (args.all_unknown or o.get("confidence") == "unidentified")]

    # A photo the model could not identify is still unidentified, so it stays
    # eligible forever and every --all-unknown run pays for it again. Cap the
    # retries: a shot with no diagnostic features will not resolve on attempt six.
    if args.max_attempts and not args.retry_exhausted:
        spent = [o for o in pending if o.get("id_attempts", 0) >= args.max_attempts]
        pending = [o for o in pending if o.get("id_attempts", 0) < args.max_attempts]
        if spent:
            print(f"Skipping {len(spent)} photo(s) already attempted {args.max_attempts}x "
                  f"(--retry-exhausted to force, --max-attempts to change).")
    if not pending and not args.collect:
        print("Nothing awaiting identification."
              + ("" if args.all_unknown else "  (--all-unknown to retry the hand-flagged ones)"))
        return
    if args.limit:
        pending = pending[: args.limit]

    # The catalogue Claude can match against, so it reuses entries instead of duplicating them.
    catalogue = "\n".join(
        f"- {s['id']}: {s['common']} ({s['scientific']})" for s in species if s["id"] != "unknown"
    )

    print(f"Identifying {len(pending)} photo(s) with {args.model} at effort={args.effort}.")
    print(f"Region: {args.region}\n")
    done = failed = rejected = reused = 0
    spend = {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0}
    cache = idcache.connect()
    ctx = {"species": species, "by_id": by_id, "catalogue": catalogue, "args": args}

    # --- collect: apply results from batches submitted on an earlier run ---
    if args.collect:
        opens = idcache.open_batches(cache)
        if not opens:
            print("No batches awaiting collection.")
            return
        counters = {"done": 0, "rejected": 0, "failed": 0, "in": 0, "out": 0}
        for bid, created, n, model, region in opens:
            b = client.messages.batches.retrieve(bid)
            if b.processing_status != "ended":
                print(f"{bid}: still {b.processing_status} — try again later.")
                continue
            print(f"Collecting {bid} ({n} photos, submitted {created}):")
            collect_batch(client, bid, obs, ctx, cache, counters)
            save(SPECIES_F, ctx["species"])
            save_obs(obs)
        cin, cout = idcache.PRICES.get(args.model, (0.0, 0.0))
        cost = (counters["in"] * cin + counters["out"] * cout) * 0.5   # batch is half price
        print(f"\nCollected {counters['done']} identified, {counters['rejected']} screened out, "
              f"{counters['failed']} failed.")
        if counters["in"]:
            print(f"{counters['in']:,} in / {counters['out']:,} out  =  ${cost:.2f} (batch rate)")
        print("Rebuild and publish:  python3 scripts/plantdb.py publish")
        return

    # --- batch: submit everything at once for half price ---
    if args.batch:
        fresh_pending = []
        for o in pending:
            hit = idcache.get(cache, o.get("hash"))
            if hit is not None and not args.ignore_cache:
                apply_result(hit["result"], o, ctx, hit.get("species_id"))
                reused += 1
            elif not o.get("hash"):
                print(f"  ! {o['file']}: no content hash, cannot batch — run without --batch")
            else:
                o["id_attempts"] = o.get("id_attempts", 0) + 1
                fresh_pending.append(o)
        if reused:
            save(SPECIES_F, ctx["species"]); save_obs(obs)
            print(f"Applied {reused} cached result(s) with no API call.")
        if not fresh_pending:
            print("Nothing left to submit.")
            return
        print(f"\nSubmitting {len(fresh_pending)} photo(s) to the Batch API (half price)...")
        ids = submit_batches(client, fresh_pending, ctx, cache)
        save_obs(obs)
        if args.no_wait:
            print(f"\n{len(ids)} batch(es) submitted. Collect when ready:")
            print("  .venv/bin/python scripts/identify.py --collect")
            return
        print("\nWaiting for results (usually well under an hour; up to 24h is allowed).")
        print("Safe to interrupt — resume with:  identify.py --collect\n")
        wait_for(client, ids)
        counters = {"done": 0, "rejected": 0, "failed": 0, "in": 0, "out": 0}
        for bid in ids:
            print(f"\nCollecting {bid}:")
            collect_batch(client, bid, obs, ctx, cache, counters)
            save(SPECIES_F, ctx["species"]); save_obs(obs)
        cin, cout = idcache.PRICES.get(args.model, (0.0, 0.0))
        cost = (counters["in"] * cin + counters["out"] * cout) * 0.5
        print(f"\nIdentified {counters['done']}, reused from cache {reused}, "
              f"screened out {counters['rejected']}, failed {counters['failed']}.")
        if counters["in"]:
            print(f"{counters['in']:,} in / {counters['out']:,} out  =  ${cost:.2f} (batch rate, 50% off)")
        print("Rebuild and publish:  python3 scripts/plantdb.py publish")
        return
    for i, o in enumerate(pending, 1):
        thumb = thumb_path(o)
        if not thumb.exists():
            print(f"  [{i}/{len(pending)}] {o['file']}: no thumbnail, skipping")
            failed += 1
            continue

        o["id_attempts"] = o.get("id_attempts", 0) + 1   # counted even if the call fails
        save_obs(obs)
        # Already paid for this image? The cache is keyed by content hash, so this
        # survives a reset of observations.json, a re-ingest under a new filename,
        # or a remove-then-re-add. Nothing is bought twice.
        cached = idcache.get(cache, o.get("hash"))
        cached_sid = None
        if cached is not None and not args.ignore_cache:
            r, cached_sid = cached["result"], cached.get("species_id")
            reused += 1
            print(f"  [{i}/{len(pending)}] {o['file']}: cached (no API call)")
        else:
            o["id_attempts"] = o.get("id_attempts", 0) + 1   # counted even if the call fails
            save_obs(obs)
            img = base64.standard_b64encode(thumb.read_bytes()).decode()
            prompt = (
                f"{describe(o)}\n\n"
                "Identify the main plant or fungus in this photograph."
            )
            try:
                resp = client.messages.create(
                    model=args.model,
                    max_tokens=MAX_TOKENS,
                    # The system block carries the instructions AND the species
                    # catalogue, and is marked cacheable. Render order is
                    # system -> messages, so everything stable must live here:
                    # the image varies per photo, and anything after it in the
                    # prefix cannot be cached.
                    system=[{
                        "type": "text",
                        "text": SYSTEM.format(region=args.region)
                                + "\n\nSpecies already in this guide — reuse one of these ids if "
                                  f"the photo shows the same organism:\n{ctx['catalogue']}",
                        "cache_control": {"type": "ephemeral"},
                    }],
                    output_config={
                        "effort": args.effort,
                        "format": {"type": "json_schema", "schema": SCHEMA},
                    },
                    messages=[{"role": "user", "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img}},
                        {"type": "text", "text": prompt},
                    ]}],
                )
            except Exception as e:
                print(f"  [{i}/{len(pending)}] {o['file']}: API error — {e}")
                failed += 1
                continue

            if resp.stop_reason == "refusal":
                print(f"  [{i}/{len(pending)}] {o['file']}: declined by safety classifier, skipping")
                failed += 1
                continue
            if resp.stop_reason == "max_tokens":
                print(f"  [{i}/{len(pending)}] {o['file']}: response truncated, skipping (raise MAX_TOKENS)")
                failed += 1
                continue

            text = next((b.text for b in resp.content if b.type == "text"), None)
            if not text:
                print(f"  [{i}/{len(pending)}] {o['file']}: empty response, skipping")
                failed += 1
                continue
            r = json.loads(text)
            fresh = resp.usage
            u = resp.usage
            spend["in"] += u.input_tokens or 0
            spend["out"] += u.output_tokens or 0
            spend["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
            spend["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0

        outcome, label = apply_result(r, o, ctx, cached_sid)
        if outcome == "rejected":
            rejected += 1
            print(f"  [{i}/{len(pending)}] {o['file']}: {label}")
            save_obs(obs)
            continue
        if cached_sid is None:
            idcache.put(cache, o.get("hash"), o["file"], r, args.model, args.region,
                        fresh, species_id=o["species_id"])
        done += 1
        print(f"  [{i}/{len(pending)}] {o['file']}: {label}  [{r.get('confidence')}]")

        save(SPECIES_F, species)   # save as we go — a crash never loses completed work
        save_obs(obs)

    cin, cout = idcache.PRICES.get(args.model, (0.0, 0.0))
    run_cost = spend["in"] * cin + spend["out"] * cout
    print(f"\nIdentified {done}, reused from cache {reused}, screened out {rejected}, failed {failed}.")
    if spend["in"] or spend["out"]:
        print(f"This run: {spend['in']:,} in / {spend['out']:,} out  =  ${run_cost:.2f}")
        if spend["cache_read"] or spend["cache_write"]:
            print(f"  prompt cache: {spend['cache_read']:,} read, {spend['cache_write']:,} written")
    if reused:
        print(f"Skipped {reused} API call(s) — already bought.")
    if done:
        print("Rebuild and publish:  python3 scripts/plantdb.py publish")


if __name__ == "__main__":
    main()
