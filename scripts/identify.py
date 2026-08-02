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
from plantdb import load_obs, save_obs, thumb_path  # noqa: E402


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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, help="only do this many (good for a first test)")
    ap.add_argument("--all-unknown", action="store_true",
                    help="also retry photos a human already looked at and left unknown")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--effort", default=EFFORT, choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--region", default=os.environ.get("PLANT_REGION", DEFAULT_REGION),
                    help=f"where these photos were taken, e.g. 'the Pacific Northwest' (default: {DEFAULT_REGION})")
    args = ap.parse_args()

    try:
        import anthropic
    except ImportError:
        sys.exit("The anthropic SDK isn't installed. Run:\n  .venv/bin/pip install anthropic\n"
                 "and call this script as  .venv/bin/python scripts/identify.py")

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        sys.exit("No API key found. Get one at https://console.anthropic.com/settings/keys then:\n"
                 "  export ANTHROPIC_API_KEY=sk-ant-...")

    client = anthropic.Anthropic()
    species, obs = load(SPECIES_F), load_obs()
    by_id = {s["id"]: s for s in species}

    pending = [o for o in obs if o["species_id"] == "unknown"
               and (args.all_unknown or o.get("confidence") == "unidentified")]
    if not pending:
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
    done = failed = rejected = 0
    for i, o in enumerate(pending, 1):
        thumb = thumb_path(o)
        if not thumb.exists():
            print(f"  [{i}/{len(pending)}] {o['file']}: no thumbnail, skipping")
            failed += 1
            continue

        img = base64.standard_b64encode(thumb.read_bytes()).decode()
        prompt = (
            f"{describe(o)}\n\n"
            "Species already in this guide — reuse one of these ids if the photo shows the same "
            f"organism:\n{catalogue}\n\n"
            "Identify the main plant or fungus in this photograph."
        )
        try:
            resp = client.messages.create(
                model=args.model,
                max_tokens=MAX_TOKENS,
                system=SYSTEM.format(region=args.region),
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
            thumb.unlink(missing_ok=True)
            rejected += 1
            print(f"  [{i}/{len(pending)}] {o['file']}: REJECTED — {reason[:60]}")
            save_obs(obs)
            continue

        # Fungi never get an edible verdict from a photo, whatever came back.
        if r["kind"] == "fungus" and r["edibility"] not in ("toxic", "unknown"):
            r["edibility"] = "unknown"
            r["cautions"] = ["Identified from a photograph only — never eat a wild mushroom on a photo ID."] \
                + r.get("cautions", [])

        if r["matches_existing_id"] and r["matches_existing_id"] in by_id:
            sid = r["matches_existing_id"]
            label = f"→ {by_id[sid]['common']}"
        else:
            sid = slugify(r["common"] or "unnamed", set(by_id))
            entry = {
                "id": sid,
                "common": r["common"] or "Unidentified",
                "scientific": r["scientific"] or "—",
                "family": r["family"] or "—",
                "kind": r["kind"],
                "edibility": r["edibility"],
                "summary": r["summary"],
                "parts": r["parts"],
                "id_marks": r["id_marks"],
                "cautions": r["cautions"],
                "lookalikes": r["lookalikes"],
                "origin_status": r.get("origin_status", "unknown"),
                "source": f"auto ({args.model})",
            }
            if r["danger"]:
                entry["danger"] = True
            species.append(entry)
            by_id[sid] = entry
            catalogue += f"\n- {sid}: {entry['common']} ({entry['scientific']})"
            flag = " ** REGULATED **" if entry["origin_status"] == "regulated" else (
                   " * invasive *" if entry["origin_status"] == "invasive" else "")
            label = f"NEW {entry['common']} [{entry['origin_status']}]{flag}"

        o["species_id"] = sid
        o["confidence"] = r["confidence"]
        o["note"] = r["note"]
        # When this record's identification was last written. The site shows it so a
        # reader can tell a fresh ID from one that has sat unrevised.
        o["identified"] = datetime.date.today().isoformat()
        also = [x for x in r.get("also_visible", []) if x in by_id and x != sid]
        if also:
            o["also"] = also
        done += 1
        u = resp.usage
        print(f"  [{i}/{len(pending)}] {o['file']}: {label}  [{r['confidence']}]  "
              f"{u.input_tokens}in/{u.output_tokens}out")

        save(SPECIES_F, species)   # save as we go — a crash never loses completed work
        save_obs(obs)

    print(f"\nIdentified {done}, screened out {rejected}, failed {failed}.")
    if done:
        print("Rebuild and publish:  python3 scripts/plantdb.py publish")


if __name__ == "__main__":
    main()
