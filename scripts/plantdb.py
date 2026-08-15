#!/usr/bin/env python3
"""
Field Guide database tool.

  python3 scripts/plantdb.py ingest ~/Pictures/some-folder   # add a whole folder of new photos
  python3 scripts/plantdb.py build                           # regenerate the app's data file
  python3 scripts/plantdb.py species                         # list species ids for tagging
  python3 scripts/plantdb.py todo                            # show photos still unidentified

Ingest is safe to re-run: photos already in the library (matched by content hash)
are skipped, so you can point it at the same folder repeatedly.
"""
import argparse, errno, hashlib, json, math, os, pathlib, re, shutil, subprocess, sys, datetime
from urllib.parse import quote

ROOT = pathlib.Path(__file__).resolve().parent.parent
PHOTOS, THUMBS, DATA = ROOT / "photos", ROOT / "thumbs", ROOT / "data"
# Both thumbnail directories are gitignored: published images are served from R2,
# so git never needs them. The split still matters — it keeps local-only images
# out of any future bundled build and away from the upload path entirely.
THUMBS_LOCAL = ROOT / "thumbs-local"
SPECIES_F, OBS_F = DATA / "species.json", DATA / "observations.json"
# Local-only records: gitignored, so they can never be committed or published.
# Which file a record lives in IS the marker — there is no flag to forget to set.
# Use this for anything shot outside the survey area, e.g. test photos from a
# populated town, where the "precise location is the deliverable" argument does
# not hold and the upstream reasons for blurring do.
LOCAL_OBS_F = DATA / "observations-local.json"
PUBCFG_F = DATA / "publish-config.json"       # tracked: where published images live
R2_MANIFEST = DATA / "r2-manifest.json"       # gitignored: what we've already uploaded
# Vestigial from upstream, where it was the gitignored full-precision copy behind
# blurred public records. Here `blur` preserves precision and coordinates are
# published as-is, so this holds nothing the tracked records do not — it is neither
# gitignored nor private, whatever the filename says. Written at ingest, never read.
PRIVATE_F = DATA / "locations-private.json"
INVASIVE_F = DATA / "invasive-reference.json"
DATA_JS = ROOT / "app" / "data.js"
EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".webp"}
THUMB_PX = 1000
COORD_DP = 6   # ~0.1 m. A survey record is only as useful as its location.

# NOTE: this is deliberately the OPPOSITE of the family field guide this was forked
# from. That project blurs coordinates to ~1 km to protect a child's walking routes.
# Sears Island is uninhabited public land: precise location IS the deliverable, because
# you cannot send a crew to treat an infestation you have located to within a kilometre.
# `verify` below enforces this direction — it fails on MISSING precision, not on precision.


def blur(v):
    """Kept for API compatibility with the upstream tool; here it preserves precision."""
    try:
        return f"{round(float(v), COORD_DP):.{COORD_DP}f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return ""


def load(p, default):
    if not p.exists():
        return default
    with open(p) as f:            # closed explicitly: CPython's refcount would get
        return json.load(f)       # there anyway, but it warns, and the noise lands
                                  # in every test run.


def save(p, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    json.dump(obj, open(p, "w"), indent=1)


def load_obs():
    """Every observation, public and local-only, tagged in memory by origin."""
    obs = load(OBS_F, [])
    for o in load(LOCAL_OBS_F, []):
        obs.append({**o, "local_only": True})
    return obs


def save_obs(obs):
    """Split back out by the local_only tag. Local records never touch OBS_F."""
    save(OBS_F, [o for o in obs if not o.get("local_only")])
    loc = [{k: v for k, v in o.items() if k != "local_only"} for o in obs if o.get("local_only")]
    if loc or LOCAL_OBS_F.exists():
        save(LOCAL_OBS_F, loc)


# A record carries two identifications: what the model said, and what a person
# confirmed on the ground. They are kept in separate fields on purpose — the
# pipeline owns species_id, a human owns everything under `verified`, and neither
# overwrites the other. That is what makes a two-way sync with an outside editor
# (a shared spreadsheet, say) conflict-free later: every field has one writer.
VERIFY_STATUS = ("confirmed", "corrected", "rejected", "revisit")

# How many verifications one unattended collection may withdraw before it stops
# and asks. Withdrawals are the hardest data here to reconstruct — each one
# represents somebody having walked out there — and in a field app withdrawing is
# a single tap, so a run of them is likelier a mistake than that many changes of mind.
MAX_UNATTENDED_CLEARS = 2


# --- Who may do what --------------------------------------------------------
# Contributors sign in to the site and write through a Cloudflare Worker, which
# drops their submissions into an R2 inbox for the pipeline to drain. The Worker
# checks these capabilities before accepting anything — and `inbox-pull` checks
# them AGAIN before applying it.
#
# That second check is not belt-and-braces pedantry. The project's central claim
# is that `verified` records only a real human field check, and a Worker is a
# deployed artefact that can be redeployed, misconfigured or compromised without
# this repository changing. Re-deciding here means the rule that a contributor
# cannot write a verification lives in the tested, reviewed, version-controlled
# half of the system, and no bug on the network can move it.
#
# THIS TABLE IS AUTHORITATIVE. worker/index.js carries a copy so it can refuse
# early with a useful message; tests/test_contributor_inbox.py reads that file
# and fails if the two ever drift apart.
ROLES = ("contributor", "verifier", "admin", "pipeline")

CAPABILITIES = {
    # Add a photograph to the survey. Every signed-in role can: the survey wants
    # more photographs from more people, and an upload is screened, dated, located
    # and identified by the same pipeline as any other, so a bad one costs nothing
    # a Drive drop would not have cost.
    "upload": ("contributor", "verifier", "admin"),
    # Record that a person stood in front of the plant. This is the one the whole
    # project turns on, so it is deliberately the narrower grant.
    "verify": ("verifier", "admin"),
    # Mark a photograph surplus within one find. A judgement about which frame
    # settles an identification — near-identical shots are often close-ups of the
    # diagnostic feature — so it sits with the people who curate the survey.
    "redundant": ("admin",),
    # Undo or overwrite something another person recorded.
    "override": ("admin",),
    # Collect the inbox and answer for what happened to it. `pipeline` is the role
    # the unattended run holds, and it is the ONLY thing it may do: the token
    # sitting in GitHub Actions secrets cannot record a field check, mark a
    # photograph surplus or upload anything. It can only carry out what a person
    # already decided. Given that `verified` is the project's central claim, the
    # credential that runs every night unattended should not be able to manufacture
    # one.
    "drain": ("pipeline", "admin"),
}


def may(role, capability):
    """Whether this role may do this thing. Unknown role or capability: no."""
    return role in CAPABILITIES.get(capability, ())


# --- Where an original actually lives ---------------------------------------
# This used to be answered "in photos/", and for photographs uploaded through the
# site that answer was wrong in the way that matters: in a cloud pipeline run
# photos/ exists only for the length of the runner, so every promise that a
# withdrawn or surplus photograph was "kept in photos/" described a directory that
# had already been destroyed.
#
# So the private inbox bucket is the archive of record for anything uploaded, and
# the original is never deleted from it — not when a photograph is marked surplus,
# not when a record is withdrawn, not when the thumbnail is pruned from the public
# bucket. `archive_key` on the record is how to find it.
#
# The other two routes keep their own archives, and neither is photos/ either:
# Drive holds the originals a contributor dropped there, which is where they
# already were, and a local `ingest` reads a folder the person still has.
def archive_of(o):
    """Where this record's original is, in words, or None if it has no archive.

    Used by anything that tells a person what a destructive-looking action will
    actually destroy, so it must never name a location that is not really there.
    """
    if o.get("archive_key"):
        return f"the contributor inbox bucket ({o['archive_key']})"
    if o.get("drive_id"):
        return "the shared Drive folder"
    return None


# --- Withdrawing a record ---------------------------------------------------
# Soft, reversible, and attributed. A record is taken off the site and its public
# thumbnail pruned, while the record itself and its original survive — the same
# shape as a surplus mark, and for the same reason: this survey does not delete
# things, and a mis-tap on a phone must not be able to destroy evidence.
#
# Distinct from `redundant`, which says "this frame is surplus to another of the
# same find". A withdrawal says the record should not be in the survey at all —
# somebody else's dog, a duplicate import, a photograph the contributor asked to
# have taken down.
def is_withdrawn(o):
    return bool((o.get("withdrawn") or {}).get("by"))


def withdrawal_problem(w):
    """Why this withdrawal will not be accepted, or None.

    A reason is required, unlike most fields here. A withdrawn record is invisible
    on the site, so the only account of why it went is this string — and "it was
    wrong" from someone who has since left the project is not an account.
    """
    if not w.get("by"):
        return "needs a name — an unattributed withdrawal is not one"
    if not (w.get("reason") or "").strip():
        return "needs a reason — it is the only record of why this left the survey"
    return None


# --- Surplus photographs ----------------------------------------------------
# Patches get photographed five to ten times. Grouping already keeps the listings
# honest (`one_per_patch`), but every one of those frames is still carried: hosted
# on R2, shipped in the site's data, shown in the species page's photo strip.
#
# A mark says: within THIS find — this species, this patch — this frame is surplus
# to the one that represents it. It is deliberately scoped that way rather than
# being a property of the image, because the same photograph can be surplus for
# the willowherb it is the seventh shot of and the only record there is of the
# bittersweet behind it.
#
# It is deliberately NOT a perceptual hash. Two frames a difference algorithm
# calls identical are routinely a habit shot and a close-up of the one feature
# that settles the identification, and throwing the second away silently destroys
# the evidence for the first. A person decides, and signs it.
#
# Nothing is deleted. The original stays in its archive (see `archive_of`), the
# record stays in observations.json with the mark on it, and the count of what the
# find is built on stays truthful. Unmark it and the next publish carries it again.
def is_redundant(o):
    """True when a person has marked this photograph surplus within its find."""
    r = o.get("redundant") or {}
    return bool(r.get("by") and r.get("of") and r.get("species_id"))


def redundancy_problem(r, o, rep, species_ids, obs=None):
    """Why this mark is not one the survey will act on, or None if it is.

    Checked at the boundary — by `inbox-pull` before applying, and by `verify`
    on everything already stored — for the same reason `verification_problem`
    is: a rule that decides what to accept must be one function, or the thing
    telling a contributor their mark landed will drift from the thing acting on
    it.

    The two scoping tests are what make a mark mean "surplus within this find"
    rather than "hide this image". Both are re-derived from the records as they
    stand now, so a mark stops applying if a re-identification moves either
    photograph to a different species, or `refresh-gps` moves one out of the
    patch. It lapses rather than quietly going on hiding a photograph of
    something else.
    """
    if not r.get("by"):
        return "needs a name — an unattributed judgement is not one"
    if not r.get("of"):
        return "needs the photograph it defers to"
    if rep is None:
        return f"defers to '{r['of']}', which is not a record here"
    if rep["file"] == o["file"]:
        return "a photograph cannot be surplus to itself"
    if is_redundant(rep):
        return (f"defers to '{rep['file']}', which is itself marked surplus — "
                "one photograph has to represent the find")
    sid = r.get("species_id")
    if not sid:
        return "needs the species it is surplus for"
    if sid not in species_ids:
        return f"unknown species id '{sid}'"
    if effective_species(o) != sid or effective_species(rep) != sid:
        return (f"both photographs must currently be records of '{sid}' — "
                "an identification has changed since this was marked")
    if not (_located(o) and _located(rep)):
        return "both photographs need coordinates — without them they cannot be one patch"
    d = metres(o, rep)
    if d > OCCURRENCE_RADIUS_M:
        return (f"{d:.1f} m apart, beyond the {OCCURRENCE_RADIUS_M} m that makes one patch — "
                "these are separate finds, each with its own photographs")

    # The mark is scoped to one species, but its EFFECT is on the whole image:
    # a surplus frame stops being carried, and everything identified in it goes
    # with it. Roughly a fifth of this survey's photographs have an `also` list,
    # and a plant caught behind the subject is frequently the only record there is
    # of it growing at that spot — for an invasive, possibly the only one there
    # will ever be. So a mark that would leave some other species with nothing
    # carried at this spot is refused, and the person is told which one.
    #
    # Deliberately conservative: it asks for another carried photograph within the
    # radius of THIS one, not anywhere in the single-link chain. Erring towards
    # refusing costs an explanation; erring the other way costs the evidence.
    if obs is not None:
        for sid in {effective_species(o), *(o.get("also") or [])} - {"unknown", r["species_id"]}:
            near = [m for m in obs
                    if m["file"] != o["file"] and not is_redundant(m) and not m.get("rejected")
                    and _located(m) and metres(o, m) <= OCCURRENCE_RADIUS_M
                    and sid in {effective_species(m), *(m.get("also") or [])}]
            if not near:
                return (f"it is the only photograph carrying '{sid}' at this spot — "
                        "marking it surplus would delete that record too")
    return None


def effective_species(o):
    """The species to believe: a human correction if there is one, else the model's."""
    v = o.get("verified") or {}
    if v.get("status") == "corrected" and v.get("species_id"):
        return v["species_id"]
    if v.get("status") == "rejected":
        return "unknown"
    return o.get("species_id", "unknown")


def is_verified(o):
    """True when a person has actually been to the spot and recorded a verdict."""
    return (o.get("verified") or {}).get("status") in VERIFY_STATUS


def thumb_dir(o):
    return THUMBS_LOCAL if o.get("local_only") else THUMBS


def thumb_path(o):
    return thumb_dir(o) / o["file"]


def withheld_reason(o):
    """Why this record is not carried on the published site, or None if it is.

    Four ways a photograph fails to earn its place, and the survey needs all of
    them answered before it will publish one:

      * It is not a photograph of vegetation. The screener says so.
      * It cannot be placed or dated. A sighting is a claim that a species was HERE,
        on THIS DAY; without both, there is nothing to send anyone to check and
        nothing to compare against a later visit. The photo is real, but it is not
        a survey record, and on the map it would be a pin with no coordinates and
        in a list an undated one.
      * Nothing in it could be identified. A habitat shot or a bark close-up is a
        fair vegetation photograph, but if no organism could be named it contributes
        no finding and only dilutes the pins that mean something.
      * A person has marked it surplus to another photograph of the same find. This
        one is different in kind from the others: the record is perfectly good
        evidence and stays in the data and in the photograph count. It is the IMAGE
        that stops being carried, because the find already has a frame representing
        it and the seventh shot of one patch costs an upload, a hosted object and a
        reader's attention for nothing.
      * A person has withdrawn it outright. Reversible, attributed, and the
        original is untouched in its archive.

    Derived rather than stored as a flag, so it corrects itself — the moment a
    re-run identifies the photo, someone unmarks a surplus frame or restores a
    withdrawn one, it publishes again with no bookkeeping to remember. Withheld
    records stay in the data and in `todo`. Nothing here deletes anything.
    """
    if is_withdrawn(o):
        w = o["withdrawn"]
        return f"withdrawn by {w['by']} — {w.get('reason', 'no reason given')}"
    if o.get("rejected"):
        return "screened out — not a photograph of vegetation"
    if is_redundant(o):
        r = o["redundant"]
        return f"marked surplus by {r['by']} — '{r['of']}' represents this find"
    if not (o.get("lat") and o.get("lon")):
        return "no location — nothing can be sent to check it"
    if not o.get("taken"):
        return "no capture date — the sighting cannot be placed in time"
    if is_verified(o) or o.get("also"):
        return None                        # a person looked, or something else in frame was named
    if o.get("species_id", "unknown") == "unknown":
        return "nothing in it could be identified"
    return None


def is_publishable(o):
    return withheld_reason(o) is None


def reviewable(o):
    """Worth putting in front of a steward: it could still become a survey record.

    An unidentified photo belongs in the sheet — someone who knows the flora can
    name it, and that is exactly what `corrected` is for. A photo with no location
    or date does not: there is no column a person could fill to fix it, so it would
    only spend a reviewer's attention on something that can never publish.

    Nor does a frame already marked surplus: the find it belongs to is in the sheet
    under the photograph that represents it, and putting the other six there too is
    the seven-walks-for-one-shrub problem `one_per_patch` exists to end.
    """
    return bool(not o.get("rejected") and not is_redundant(o) and not is_withdrawn(o)
                and o.get("lat") and o.get("lon") and o.get("taken"))


def public_obs():
    """Only what may be published: public file, minus anything that says nothing."""
    return [o for o in load(OBS_F, []) if is_publishable(o)]


def in_area(o, area):
    try:
        lat, lon = float(o["lat"]), float(o["lon"])
    except (TypeError, ValueError, KeyError):
        return None            # no coordinates — can't place it either way
    return (area["lat_min"] <= lat <= area["lat_max"]
            and area["lon_min"] <= lon <= area["lon_max"])


def area_check(obs):
    """Split published records by whether they fall inside the survey area.

    Returns (area, inside, outside, enforcing) or None when no area is configured.

    Enforcement is deliberately automatic. `enforce: "auto"` keeps this advisory
    while the published set is stand-in data from elsewhere, and makes it binding
    the moment the first genuine in-area record lands — so the arrival of real
    survey photos is what forces the placeholder data out, rather than someone
    remembering to flip a switch. Set true or false to decide explicitly.
    """
    area = load(PUBCFG_F, {}).get("survey_area")
    if not area or "lat_min" not in area:
        return None
    inside, outside = [], []
    for o in obs:
        r = in_area(o, area)
        if r is True:
            inside.append(o)
        elif r is False:
            outside.append(o)
    mode = area.get("enforce", "auto")
    enforcing = bool(inside) if mode == "auto" else bool(mode)
    return area, inside, outside, enforcing


def recorded_species(species, obs):
    """Only the species some photograph in this set actually shows.

    The catalogue is two things at once, and the site should only ever present one
    of them. To the identifier it is vocabulary — 40-odd species carried over from
    an inland roadside walk upstream, there so the model can match a plant instead
    of inventing a name for it. To a reader of a page headed "Sears Island Flora
    Survey" it looks like an inventory of the island.

    Most of that vocabulary has never been photographed here, and four entries are
    flagged invasive or regulated. A reviewer filtering for invasives would see them
    listed beside genuine finds — a claim about contested ground that nothing in
    this survey supports. So publishing shows what was photographed, and the rest
    stays in data/species.json doing the job it is actually for.

    `unknown` is always kept: the app falls back to it for any record whose species
    is missing, and without it those records render as an error instead of a photo.
    """
    ref = {"unknown"}
    for o in obs:
        ref.add(o.get("species_id", "unknown"))
        ref.add(effective_species(o))
        ref.update(o.get("also") or [])
    return [s for s in species if s["id"] in ref]


# --- Occurrences ------------------------------------------------------------
# An observation is one photograph. An OCCURRENCE is one species growing in one
# place — which is what a land manager treats, what a state database records, and
# what someone walks out to check. Seven photographs of one willowherb patch are
# seven observations and one occurrence, and the difference matters: submitted per
# photograph they would read as seven infestations.
#
# Derived from the records rather than stored, like everything else here, so a
# photograph taken next year at the same spot joins the occurrence by being where
# it is. There is no membership to maintain and nothing to forget to update.
#
# 10 metres, from the data: photographs of one patch sit 0–2.1 m apart, and the
# nearest genuinely separate site is 10.7 m away. The threshold sits in the gap.
OCCURRENCE_RADIUS_M = 10


def metres(a, b):
    """Distance between two records in metres. Flat-earth, which at this scale is
    accurate to well under the GPS error it is comparing."""
    la1, lo1 = float(a["lat"]), float(a["lon"])
    la2, lo2 = float(b["lat"]), float(b["lon"])
    dy = (la2 - la1) * 111_320
    dx = (lo2 - lo1) * 111_320 * math.cos(math.radians((la1 + la2) / 2))
    return math.hypot(dx, dy)


def _located(o):
    return bool(o.get("lat") and o.get("lon"))


def occurrences(obs, radius=OCCURRENCE_RADIUS_M, subject_only=False):
    """Every (species, place) pair the survey has evidence for.

    A photograph counts towards a species if it is the subject OR if the species
    was identified in the background — a plant caught behind the subject is still
    a real record of it growing at that spot, and for an invasive it may be the
    only record there is.

    Grouping is single-link: a photograph joins a group if it is within `radius`
    of ANY member, so a straggling thicket photographed along its length chains
    into one occurrence instead of splitting at every stride.
    """
    by_species = {}
    for o in obs:
        if not _located(o) or o.get("rejected") or is_withdrawn(o):
            continue
        # `subject_only` for surfaces that list RECORDS rather than species: a
        # photograph belongs to one row there, under whatever it is a photograph
        # of, or it would appear once per species identified in it.
        sids = ({effective_species(o)} if subject_only
                else {effective_species(o), *(o.get("also") or [])})
        for sid in sids:
            if sid != "unknown":
                by_species.setdefault(sid, []).append(o)

    out = []
    for sid, records in by_species.items():
        groups = []
        for o in sorted(records, key=lambda r: r.get("taken", "")):
            touching = [g for g in groups if any(metres(o, m) <= radius for m in g)]
            if touching:
                first = touching[0]
                first.append(o)
                for other in touching[1:]:      # this photo bridges two groups
                    first.extend(other)
                    groups.remove(other)
            else:
                groups.append([o])
        for g in groups:
            g.sort(key=lambda r: r.get("taken", ""))
            out.append(_occurrence(sid, g))
    out.sort(key=lambda x: (RANK.get(x["origin_status"], 9), x["species_id"]))
    return out


def one_per_patch(obs, radius=OCCURRENCE_RADIUS_M):
    """One record per (species, patch), plus how many photographs stand behind it.

    The rule the whole survey is listed by. A patch photographed seven times is one
    thing growing in one place: seven rows in a steward's sheet is seven walks to
    verify one shrub, and seven lines in a report overstates what is on the ground.
    Every photograph is kept — they are the evidence — but only one of them
    represents the find anywhere that enumerates records.

    Records with no location cannot be grouped, so each stands alone. That is right:
    without coordinates there is no way to know whether two of them are the same
    plant, and merging on a guess would invent a finding.
    """
    grouped, seen = [], set()
    for x in occurrences(obs, radius, subject_only=True):
        grouped.append((x["anchor"], len(x["members"]), x))
        seen.update(o["file"] for o in x["members"])
    for o in obs:
        if o["file"] not in seen:
            grouped.append((o, 1, None))
    return grouped


def occurrence_payload(obs):
    """Occurrences, trimmed to what the site needs to ask someone for help.

    The app cannot recompute this: clustering lives in one place so the checklist a
    volunteer reads and the record the state receives describe the same find.

    Pass the surplus records in along with the carried ones. `n` counts what the
    site can actually show and `files` lists only those, but `surplus` reports how
    many further frames a person marked — so a find photographed seven times and
    curated down to one still says so, instead of quietly presenting as a find
    somebody photographed once.
    """
    out = []
    for x in occurrences(obs):
        carried = [o for o in x["members"] if not is_redundant(o)]
        out.append({"species_id": x["species_id"],
                    "lat": round(x["lat"], 6), "lon": round(x["lon"], 6),
                    "n": len(carried), "surplus": len(x["surplus"]),
                    "spread_m": round(x["spread_m"], 1),
                    "first_seen": x["first_seen"], "last_seen": x["last_seen"],
                    "confirmed": x["confirmed"], "confirmed_by": x["confirmed_by"],
                    "checked_on": x["checked_on"],
                    "confirming_photos": len(x["confirming_photos"]),
                    # EVERY member, surplus included, though `n` counts only the
                    # carried ones. This list is how the app knows which patch a
                    # photograph belongs to, and a surplus frame that is not in it
                    # detaches from its own find and renders as a lone photograph
                    # somewhere else on the page — which is the opposite of what
                    # marking it was for. Published data contains no surplus
                    # records at all, so there the extra names resolve to nothing
                    # and are simply never looked up.
                    "files": [o["file"] for o in x["members"]]})
    return out


def surplus_records(obs):
    """Records kept off the site ONLY because a person marked them surplus.

    Distinct from "every record with a mark on it": a photograph that is also
    undated, or that the screener rejected, is withheld on its own merits and
    counting it as curation would overstate what the marking actually did.
    """
    return [o for o in obs if is_redundant(o)
            and withheld_reason({k: v for k, v in o.items() if k != "redundant"}) is None]


def _occurrence(sid, members):
    """One occurrence, summarised. `anchor` is the earliest photograph — the one to
    name when confirming, and stable while it exists.

    Earliest, but never one somebody marked surplus: the anchor is what represents
    the find in every list and what a steward is asked to go and check, so it has
    to be a frame that is actually carried. A find whose every photograph is marked
    falls back to the earliest, which keeps the find visible rather than dropping
    it — and `verify` reports that state, because it means the mark that was
    supposed to leave one representative left none.
    """
    species = {s["id"]: s for s in enriched_species()}.get(sid, {})
    verdicts = [o["verified"] for o in members
                if (o.get("verified") or {}).get("status") in ("confirmed", "corrected")]
    dates = [o["taken"][:10] for o in members if o.get("taken")]
    # A photograph taken on or after the day someone confirmed it is the evidence
    # that confirmation rests on. Recorded, never required — a steward who went and
    # looked has still been there, phone or no phone.
    checked_on = min((v.get("date", "") for v in verdicts), default="")
    return {
        "species_id": sid,
        "common": species.get("common", sid),
        "scientific": species.get("scientific", ""),
        "origin_status": species.get("origin_status", "unknown"),
        "origin_note": species.get("origin_note", ""),
        "id_marks": species.get("id_marks") or [],
        "lookalikes": species.get("lookalikes") or [],
        "members": members,
        "anchor": next((m for m in members if not is_redundant(m)), members[0]),
        "surplus": [m for m in members if is_redundant(m)],
        "lat": sum(float(o["lat"]) for o in members) / len(members),
        "lon": sum(float(o["lon"]) for o in members) / len(members),
        "spread_m": max((metres(a, b) for a in members for b in members), default=0.0),
        "first_seen": min(dates, default=""),
        "last_seen": max(dates, default=""),
        "confirmed": bool(verdicts),
        "confirmed_by": verdicts[0].get("by") if verdicts else "",
        "checked_on": checked_on,
        "confirming_photos": [o for o in members
                              if checked_on and o.get("taken", "")[:10] >= checked_on],
    }


def enriched_species():
    """Species with regulatory status applied from the reference list."""
    species = load(SPECIES_F, [])
    cls = load(INVASIVE_F, {}).get("classification", {})
    for sp in species:
        if sp["id"] in cls:
            sp["origin_status"] = cls[sp["id"]]["status"]
            if cls[sp["id"]].get("note"):
                sp["origin_note"] = cls[sp["id"]]["note"]
        sp.setdefault("origin_status", "unknown")
    return species


def sha(path, blocks=8):
    """Hash the first ~512KB — plenty to distinguish photos, and fast."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for _ in range(blocks):
            b = f.read(65536)
            if not b:
                break
            h.update(b)
    return h.hexdigest()[:16]


# EXIF is read straight out of the file. The obvious alternative, `mdls`, reads
# Spotlight's index rather than the file — and indexing of a freshly written copy is
# asynchronous, so ingest queried photos Spotlight had not seen yet and silently got
# null coordinates for everything. For a survey where location IS the deliverable, a
# silent empty is the worst possible failure, so this parses the bytes directly.
_FMT = {1: ("B", 1), 2: ("s", 1), 3: ("H", 2), 4: ("I", 4), 5: ("II", 8),
        6: ("b", 1), 7: ("s", 1), 9: ("i", 4), 10: ("ii", 8)}


def _ifd(d, base, off, order):
    """Read one IFD, returning {tag: value}."""
    import struct
    out = {}
    if off + 2 > len(d):
        return out
    n, = struct.unpack(order + "H", d[off:off + 2])
    for i in range(n):
        e = off + 2 + i * 12
        if e + 12 > len(d):
            break
        tag, typ, cnt = struct.unpack(order + "HHI", d[e:e + 8])
        if typ not in _FMT:
            continue
        code, size = _FMT[typ]
        total = size * cnt
        if total > 4:
            ptr, = struct.unpack(order + "I", d[e + 8:e + 12])
            raw = d[base + ptr: base + ptr + total]
        else:
            raw = d[e + 8:e + 8 + total]
        if len(raw) < total:
            continue
        if typ in (2, 7):
            out[tag] = raw.split(b"\x00")[0].decode("ascii", "replace")
        elif typ in (5, 10):
            vals = []
            for j in range(cnt):
                num, den = struct.unpack(order + ("II" if typ == 5 else "ii"), raw[j * 8:(j + 1) * 8])
                vals.append(num / den if den else 0.0)
            out[tag] = vals
        else:
            out[tag] = struct.unpack(order + code * cnt, raw)[0] if cnt == 1 else None
    return out


def _dms(vals, ref):
    """GPS coordinates are stored as degrees/minutes/seconds rationals."""
    if not vals or len(vals) < 3:
        return ""
    deg = vals[0] + vals[1] / 60 + vals[2] / 3600
    if str(ref).upper() in ("S", "W"):
        deg = -deg
    return f"{deg:.{COORD_DP}f}".rstrip("0").rstrip(".")


def exif_of(path):
    import struct
    taken = lat = lon = ""
    try:
        d = path.read_bytes()
        i = 2
        while i < len(d) - 3 and d[i] == 0xFF:      # walk JPEG segments to APP1
            m, seg = d[i + 1], int.from_bytes(d[i + 2:i + 4], "big")
            if m == 0xE1 and d[i + 4:i + 10] == b"Exif\x00\x00":
                tiff = i + 10
                order = "<" if d[tiff:tiff + 2] == b"II" else ">"
                ifd0_off, = struct.unpack(order + "I", d[tiff + 4:tiff + 8])
                ifd0 = _ifd(d, tiff, tiff + ifd0_off, order)
                if 0x8825 in ifd0:                  # GPS IFD pointer
                    g = _ifd(d, tiff, tiff + ifd0[0x8825], order)
                    lat = _dms(g.get(2), g.get(1, "N"))
                    lon = _dms(g.get(4), g.get(3, "E"))
                if 0x8769 in ifd0:                  # Exif IFD pointer
                    ex = _ifd(d, tiff, tiff + ifd0[0x8769], order)
                    taken = ex.get(0x9003) or ex.get(0x9004) or ""
                taken = taken or ifd0.get(0x0132) or ""
                break
            if m in (0xDA, 0xD9):
                break
            i += 2 + seg
    except Exception:
        pass
    if taken:
        # EXIF writes "YYYY:MM:DD HH:MM:SS"; the rest of the tool wants dashes.
        taken = taken.replace(":", "-", 2) + " +0000"
    # No fallback to the file's mtime. That is when the file was written to this
    # disk — for anything downloaded from Drive, the moment we downloaded it — and
    # recording it as `taken` had the site print "Photographed 2026-08-02" over a
    # photograph whose date nobody knows. It was also stamped "+0000" while being
    # read in local time, so it was wrong twice. A survey that will not invent a
    # species must not invent a date either; blank is the true answer and every
    # consumer already handles it.
    return {"taken": taken, "lat": lat, "lon": lon}


def strip_exif(path):
    """Remove EXIF/GPS/XMP/comment segments from a JPEG, in place.

    sips copies metadata through when it resizes, so a thumbnail made from a
    phone photo still carries the exact coordinates it was shot at. Thumbnails
    are the files that get published, so they must not.

    Walks the JPEG segment markers and drops APP1-APP15 (EXIF, XMP, IPTC...)
    and COM. APP0/JFIF stays — it holds only pixel-density info.
    """
    d = path.read_bytes()
    if d[:2] != b"\xff\xd8":
        return False  # not a JPEG; leave it alone
    out, i = bytearray(d[:2]), 2
    while i < len(d) - 1:
        if d[i] != 0xFF:
            out += d[i:]
            break
        m = d[i + 1]
        if m == 0xFF:            # fill byte
            out += d[i:i + 1]
            i += 1
            continue
        if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:   # standalone markers
            out += d[i:i + 2]
            i += 2
            continue
        if m in (0xDA, 0xD9):    # start of scan / end of image — copy the rest verbatim
            out += d[i:]
            break
        seg_len = int.from_bytes(d[i + 2:i + 4], "big")
        if seg_len < 2:
            out += d[i:]
            break
        if not (0xE1 <= m <= 0xEF or m == 0xFE):     # keep everything except APP1-15 and COM
            out += d[i:i + 2 + seg_len]
        i += 2 + seg_len
    path.write_bytes(bytes(out))
    return True


def to_jpeg(src, dst, max_px=None):
    """Convert (and optionally shrink) an image to JPEG. True if dst was written.

    Pillow first, `sips` second. This used to be sips alone, which is part of macOS
    and does not exist on a Linux CI runner — so in the cloud every thumbnail failed
    silently, nothing could be identified (identify reads the thumbnail), and the
    whole scheduled pipeline was an expensive no-op. Pillow covers the runner;
    sips stays as the fallback for a machine without it, which is how this ran for
    its first year.

    HEIC needs pillow-heif, which iPhones make this worth having: register it when
    present and let the sips fallback take HEIC on a Mac without it.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image, ImageOps
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except ImportError:
            pass
        with Image.open(src) as im:
            # Apply the orientation tag before it is stripped, or every photo an
            # iPhone recorded sideways stays sideways with nothing left to say so.
            im = ImageOps.exif_transpose(im).convert("RGB")
            if max_px:
                im.thumbnail((max_px, max_px))
            im.save(dst, "JPEG", quality=88)
        if dst.exists():
            return True
    except ImportError:
        pass                      # no Pillow — fall through to sips
    except Exception as e:
        print(f"  ! {src.name}: {type(e).__name__} reading image ({e}); trying sips")

    cmd = ["sips"] + (["-Z", str(max_px)] if max_px else []) + \
          ["-s", "format", "jpeg", str(src), "--out", str(dst)]
    try:
        r = subprocess.run(cmd, capture_output=True)
    except FileNotFoundError:
        return False              # neither Pillow nor sips; caller reports it
    return r.returncode == 0 and dst.exists()


def have_thumbnailer():
    """Is there any way to make a thumbnail on this machine?"""
    try:
        import PIL  # noqa: F401
        return "Pillow"
    except ImportError:
        return "sips" if shutil.which("sips") else None


def make_thumb(src, dst):
    if not to_jpeg(src, dst, THUMB_PX):
        return False
    strip_exif(dst)
    return True


def _apply_fallback(rec, fallback):
    """Fill in what the photograph did not carry, and say when that happened.

    Separated from `ingest_file` so the rule can be tested without a filesystem,
    because it is a judgement rather than plumbing: it decides when the survey is
    willing to state a location it did not get from the photograph.

    An empty string counts as absent — that is precisely what a missing EXIF tag
    reads as by the time it reaches here.
    """
    fb = {k: v for k, v in (fallback or {}).items() if v not in (None, "")}
    # All or nothing. Latitude from one source and longitude from another would
    # place the record somewhere neither of them ever saw.
    if fb.get("lat") and fb.get("lon") and not (rec.get("lat") and rec.get("lon")):
        rec["lat"], rec["lon"] = blur(fb["lat"]), blur(fb["lon"])
        # Labelled, and only ever when the substitution actually happened. The
        # record is now stating a location that did not come from the photograph,
        # and a survey whose deliverable IS location has to say so — a coordinate
        # inherited from the find someone attached this to is a different claim
        # from one the camera recorded, even when it is the better of the two.
        rec["location_source"] = fb.get("source") or "inherited"
        if fb.get("of"):
            rec["location_from"] = fb["of"]
    if fb.get("taken") and not rec.get("taken"):
        rec["taken"] = fb["taken"]
    return rec


def ingest_file(src, batch, obs, known, local=False, extra=None, fallback=None):
    """Bring one image into the library. Returns the new record, or None.

    Factored out of `cmd_ingest` so a photograph uploaded from the field goes
    through exactly this path and not a shortcut around it — same content-hash
    dedupe, same conversion, same thumbnail, same EXIF read. A field upload is a
    photograph like any other; the only thing special about it is who handed it
    over, and that goes in `extra`.

    `fallback` supplies a location and date for the common case that the browser
    stripped the EXIF on the way up — which it usually does, and which is why
    `check-photos` exists at all. It is a fallback in the strict sense: anything
    the photograph itself carried wins, and an empty string counts as absent
    because that is exactly what a missing EXIF tag reads as here.

    When the fallback location is the one actually used, the record says so in
    `location_source`. That field is the whole reason this is acceptable: a fix
    the phone took when someone pressed send is not the same claim as a fix the
    camera recorded at the shutter, and a survey that refuses to invent a species
    or a date must not quietly present one as the other.
    """
    h = sha(src)
    if h in known:
        return None
    stem = src.stem
    dest = PHOTOS / (stem + ".jpg")
    n = 1
    while dest.exists():
        dest = PHOTOS / f"{stem}-{n}.jpg"
        n += 1
    PHOTOS.mkdir(exist_ok=True)
    if src.suffix.lower() in (".jpg", ".jpeg"):
        shutil.copy2(src, dest)
    elif not to_jpeg(src, dest):
        print(f"  ! could not convert {src.name} — skipped")
        return None
    # No thumbnail, no record. identify.py reads the thumbnail, publish uploads
    # it, and the site shows it, so a record without one is a row that can never
    # become anything. Leaving it out means the photo is simply retried on the
    # next run instead of sitting in the survey as a permanent blank.
    if not make_thumb(dest, (THUMBS_LOCAL if local else THUMBS) / dest.name):
        print(f"  ! could not thumbnail {dest.name} — skipped, will retry next run")
        dest.unlink(missing_ok=True)
        return None
    e = exif_of(dest)
    # Both copies are full precision here — see PRIVATE_F above. The sidecar is
    # kept only so a record's original coordinates survive an edit to
    # observations.json; it is not a privacy boundary in this fork.
    private = load(PRIVATE_F, {})
    private[dest.name] = {"lat": e["lat"], "lon": e["lon"], "taken": e["taken"]}
    save(PRIVATE_F, private)
    rec = {"id": dest.stem, "file": dest.name, "species_id": "unknown",
           "confidence": "unidentified", "note": "", "taken": e["taken"],
           "lat": blur(e["lat"]), "lon": blur(e["lon"]), "batch": batch, "hash": h}
    _apply_fallback(rec, fallback)
    for k, v in (extra or {}).items():
        if v not in (None, ""):
            rec[k] = v
    if local:
        rec["local_only"] = True
    obs.append(rec)
    known.add(h)
    print(f"  + {dest.name}")
    return rec


def cmd_ingest(args):
    src_root = pathlib.Path(os.path.expanduser(args.folder)).resolve()
    if not src_root.is_dir():
        sys.exit(f"Not a folder: {src_root}")

    obs = load_obs()
    known = {o.get("hash") for o in obs if o.get("hash")}
    # hash anything already in the library that predates hashing
    for o in obs:
        if not o.get("hash"):
            p = PHOTOS / o["file"]
            if p.exists():
                o["hash"] = sha(p)
                known.add(o["hash"])

    found = sorted(p for p in src_root.rglob("*") if p.suffix.lower() in EXTS and p.is_file())
    if not found:
        sys.exit(f"No images found under {src_root}")

    batch = args.batch or src_root.name
    added, skipped = 0, 0
    for src in found:
        if sha(src) in known:
            skipped += 1
            continue
        if ingest_file(src, batch, obs, known, local=args.local):
            added += 1

    obs.sort(key=lambda o: o.get("taken", ""))
    save_obs(obs)
    cmd_build(args)
    dest_note = f" into {LOCAL_OBS_F.name} (local only, never published)" if args.local else ""
    print(f"\nAdded {added} new photo(s){dest_note}, skipped {skipped} already in the library.")

    # Flag an out-of-area batch here, while it is still one command to undo, rather
    # than at publish time after it has been identified and uploaded.
    area = load(PUBCFG_F, {}).get("survey_area")
    if area and added and not args.local:
        fresh = [o for o in obs if o.get("batch") == batch]
        out = [o for o in fresh if in_area(o, area) is False]
        if out:
            name = area.get("name", "the survey area")
            print(f"\n  ! {len(out)} of {len(fresh)} photo(s) in this batch are outside {name}.")
            print(f"    If they aren't survey records, re-ingest with --local, or remove them:")
            print(f"      python3 scripts/plantdb.py remove --batch {batch} --yes")
    if added:
        print(f'They are tagged "unknown" — run `python3 scripts/plantdb.py todo` to see what needs identifying.')


def cmd_build(args):
    """Regenerate app/data.js for LOCAL viewing — includes local-only records.

    app/data.js is gitignored precisely because it merges them in. The published
    copy is written separately by `publish` and contains public records only.
    """
    species = enriched_species()
    obs = load_obs()
    ids = {s["id"] for s in species}
    for o in obs:
        if o["species_id"] not in ids:
            print(f"  ! {o['file']} references unknown species '{o['species_id']}' — falling back to 'unknown'")
            o["species_id"] = "unknown"
        # The app groups by what we currently believe, which is the human's verdict
        # where there is one. `species_id` stays as the model's original answer so
        # the site can show both — "AI said X, confirmed as Y".
        o["effective_species_id"] = effective_species(o)
        o["is_verified"] = is_verified(o)
        # Surplus frames are absent from the published data entirely. They survive
        # into the LOCAL build so `serve` shows what curation actually did, which
        # is the only place anyone can see a marked frame at all.
        o["is_redundant"] = is_redundant(o)
    # Tell the app where each thumbnail actually lives, so it doesn't have to know
    # the tracked/local split. publish() overwrites this with the published layout.
    for o in obs:
        o["thumb"] = f"{thumb_dir(o).name}/{o['file']}"
    DATA_JS.parent.mkdir(parents=True, exist_ok=True)
    # Same filter publish applies, so previewing with `serve` shows what a reader
    # will see rather than the full identification vocabulary. Local-only records
    # count as recorded here — locally they are exactly what you are checking.
    species = recorded_species(species, obs)
    payload = {"species": species, "observations": obs,
               "occurrences": occurrence_payload(obs),
               "generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}
    notice = load(PUBCFG_F, {}).get("notice")
    if notice:
        payload["notice"] = notice
    # Where a signed-in contributor submits. Not a secret and deliberately tracked,
    # for the same reason `r2_public_base` is: the deploy runner has to build a
    # working page without ever holding a credential. Absent, and the site is
    # exactly the read-only survey it was before contributor mode existed.
    if (endpoint := load(PUBCFG_F, {}).get("contributor_endpoint")):
        payload["contributor_endpoint"] = endpoint
    DATA_JS.write_text("window.PLANT_DB = " + json.dumps(payload, indent=1) + ";\n")
    n_local = sum(1 for o in obs if o.get("local_only"))
    extra = f" ({n_local} local-only, never published)" if n_local else ""
    print(f"Built {DATA_JS.relative_to(ROOT)} — {len(species)} species, {len(obs)} photos{extra}.")


RANK = {"regulated": 0, "invasive": 1, "unknown": 2, "introduced": 3, "native": 4}


def cmd_invasives(args):
    """Survey report: non-native and regulated species, with when and where they were seen."""
    ref = load(INVASIVE_F, {})
    cls = ref.get("classification", {})
    species = {s["id"]: s for s in load(SPECIES_F, [])}
    obs = load_obs()

    # A species is "seen" in a photo if it is the subject or merely visible in it.
    sightings = {}
    for o in obs:
        for sid in [effective_species(o)] + o.get("also", []):
            sightings.setdefault(sid, []).append(o)

    # Group each species' photographs into the patches they were taken of, so the
    # survey report counts finds on the ground rather than shutter presses.
    sightings = {sid: one_per_patch(shots) for sid, shots in sightings.items()}

    rows = []
    for sid, shots in sightings.items():
        st = cls.get(sid, {}).get("status", "unknown")
        if st in ("native",) and not args.all:
            continue
        if sid == "unknown":
            continue
        rows.append((RANK.get(st, 9), st, sid, shots, cls.get(sid, {}).get("note", "")))
    rows.sort(key=lambda r: (r[0], -len(r[3])))

    label = {"regulated": "REGULATED (Do Not Sell list)", "invasive": "INVASIVE (not regulated)",
             "introduced": "introduced, naturalized", "unknown": "status undetermined",
             "native": "native"}
    print(f"Survey report — {len(obs)} photos\n")
    current = None
    for _, st, sid, shots, note in rows:
        if st != current:
            current = st
            print(f"\n=== {label.get(st, st).upper()} ===\n")
        sp = species.get(sid, {})
        print(f"  {sp.get('common', sid)}  ({sp.get('scientific','?')})")
        if note:
            print(f"    {note}")
        for o, n, _ in shots:
            where = f"{o['lat']}, {o['lon']}" if o.get("lat") else "no location"
            sec = " [background]" if effective_species(o) != sid else ""
            v = o.get("verified") or {}
            mark = f"  ✓ {v['status']} by {v.get('by','?')} {v.get('date','')}" if is_verified(o) \
                   else "  · UNVERIFIED"
            more = f"  ({n} photographs)" if n > 1 else ""
            print(f"      {o.get('taken','')[:16]}  {where}  {o['file'][:8]}…{sec}{more}{mark}")
        print()

    counts = {}
    for _, st, _, _, _ in rows:
        counts[st] = counts.get(st, 0) + 1
    print("Summary: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items(), key=lambda x: RANK.get(x[0], 9))))
    nv = sum(1 for _, _, _, shots, _ in rows for o, _, _ in shots if not is_verified(o))
    if nv:
        print(f"\n{nv} of these sightings have NOT been checked by a person. "
              f"See: plantdb.py unverified")
    reg = [r for r in rows if r[1] == "regulated"]
    if reg:
        print(f"\n{len(reg)} regulated species found. These are the reportable ones —")
        print("verify each in person against the current Maine DACF list before reporting.")


def cmd_scrub(args):
    """Strip EXIF from every thumbnail and blur every tracked coordinate.

    Idempotent — safe to run any time. Run it before the first push, and any
    time you're unsure what's in the repo.
    """
    stripped = 0
    all_thumbs = sorted(THUMBS.glob("*.jpg")) + sorted(THUMBS_LOCAL.glob("*.jpg"))
    for t in all_thumbs:
        before = t.stat().st_size
        if strip_exif(t) and t.stat().st_size != before:
            stripped += 1
    print(f"Thumbnails: stripped metadata from {stripped} of {len(all_thumbs)}.")

    print("Coordinates: left at full precision — this is a survey, location is the point.")
    cmd_build(args)
    cmd_verify(args)


def cmd_verify(args):
    """Data-quality check for a survey.

    Two things are enforced, and note that the SECOND is the reverse of the
    upstream family-guide version of this tool:

      1. Published thumbnails carry no EXIF. Still true here — the useful GPS is
         extracted into the JSON, and leaving EXIF in the image only exposes
         contributor camera serials and device identifiers for no benefit.
      2. Observations DO carry precise coordinates. A sighting without a location
         is close to worthless for a survey, so it is reported as a defect.
    """
    import re
    problems, warnings = [], []

    thumbs = sorted(THUMBS.glob("*.jpg")) + sorted(THUMBS_LOCAL.glob("*.jpg"))
    for t in thumbs:
        head = t.read_bytes()[:8192]
        if b"Exif" in head or b"http://ns.adobe.com/xap" in head:
            problems.append(f"{t.relative_to(ROOT)} still has an EXIF/XMP segment")

    obs = load_obs()
    missing = [o["file"] for o in obs if not o.get("lat") or not o.get("taken")]
    imprecise = [o["file"] for o in obs
                 if o.get("lat") and len(o["lat"].split(".")[-1]) < 4]
    for f in missing:
        warnings.append(f"{f} has no location and/or no capture date — withheld from the site")
    for f in imprecise:
        problems.append(f"{f} has a coordinate rounded below survey precision")

    # Marks that no longer hold. A mark says "surplus to THAT photograph of THIS
    # species, in THIS patch" — and all three can move underneath it when a re-run
    # re-identifies a photo or `refresh-gps` recovers a better fix. A lapsed mark
    # is a photograph being kept off the site for a reason that stopped being true,
    # so it is reported rather than left to quietly go on hiding one.
    by_file = {o["file"]: o for o in obs}
    ids = {s["id"] for s in load(SPECIES_F, [])}
    for o in obs:
        if not is_redundant(o):
            continue
        r = o["redundant"]
        if (why := redundancy_problem(r, o, by_file.get(r.get("of")), ids, obs)):
            warnings.append(f"{o['file']} is marked surplus, but that no longer holds: {why}")
    # A find every one of whose photographs is marked has no representative left.
    # `_occurrence` falls back to the earliest so the find stays visible, but the
    # marking did not do what the person doing it meant it to do.
    for x in occurrences([o for o in obs if not o.get("rejected")]):
        if x["members"] and all(is_redundant(m) for m in x["members"]):
            warnings.append(f"every photograph of {x['common']} at "
                            f"{x['lat']:.5f}, {x['lon']:.5f} is marked surplus — "
                            "the find has no photograph representing it")

    # Records published under a survey's name should be from that survey.
    ac = area_check(public_obs())
    if ac:
        area, inside, outside, enforcing = ac
        mode = area.get("enforce", "auto")
        name = area.get("name", "the survey area")
        if outside and enforcing:
            problems.append(f"{len(outside)} published record(s) fall outside {name}")
            for o in outside[:3]:
                problems.append(f"  {o['file'][:12]}… at {o.get('lat')}, {o.get('lon')}")
        elif outside and mode == "auto":
            # Not a `warnings` entry — that list is specifically about missing coordinates.
            print(f"Note: {len(outside)} published record(s) are outside {name}. Tolerated "
                  f"because no in-area record exists yet, so this is still stand-in data.")
            print(f"      The check becomes binding as soon as the first {name} photo "
                  f"is published.")
        elif outside:
            print(f"Note: {len(outside)} published record(s) are outside {name}; the area "
                  f"check is switched off (survey_area.enforce = false).")
        if inside:
            print(f"{len(inside)}/{len(inside) + len(outside)} published record(s) are within {name}.")

    if problems:
        print("CHECK FAILED:")
        for p in problems[:10]:
            print(f"  ! {p}")
        if len(problems) > 10:
            print(f"  ... and {len(problems) - 10} more")
        if ac and ac[3] and ac[2]:
            batches = sorted({o.get("batch", "") for o in ac[2]} - {""})
            print("\nOut-of-area records are usually leftover stand-in data. To retire them:")
            for b in batches or ["<batch>"]:
                print(f"  python3 scripts/plantdb.py remove --batch {b} --yes")
            print("Or widen survey_area in data/publish-config.json if they belong here.")
        sys.exit(1)
    if warnings:
        print(f"{len(warnings)} record(s) need a look:")
        for w in warnings[:5]:
            print(f"  - {w}")
        if len(warnings) > 5:
            print(f"  ... and {len(warnings) - 5} more")
    # Say how many were examined. Images live on R2 and never enter git, so on a CI
    # runner thumbs/ is usually empty — and "thumbnails carry no EXIF" printed after
    # inspecting nothing reads like a passed privacy audit when none was performed.
    if not thumbs:
        print("Note: no local thumbnails to inspect, so the EXIF check examined nothing.")
        print("      Images are on R2; run `verify` where the thumbnails are to audit them.")
    else:
        print(f"Inspected {len(thumbs)} thumbnail(s) for EXIF/XMP.")
    print(f"Check passed — thumbnails carry no EXIF; "
          f"{len(obs) - len(missing)}/{len(obs)} records have survey-grade coordinates.")


def cmd_publish(args):
    """Assemble a public/ folder: app + thumbnails + survey data.

    Coordinates are published at full precision — see the note at the top of this
    file. Screened-out and unidentifiable photos are withheld entirely.
    """
    cmd_build(args)
    pub = ROOT / "public"
    if pub.exists():
        shutil.rmtree(pub)
    (pub / "app").mkdir(parents=True)
    shutil.copy2(ROOT / "index.html", pub / "index.html")

    # Built from the source files, NOT from app/data.js — that file deliberately
    # merges in local-only records, and reading it back would republish them.
    # `is_publishable` excludes two kinds of photo: one the screener rejected (not
    # vegetation — someone's camera roll spilling in), and one nothing could be
    # named in. Neither the record nor its thumbnail belongs on a public site.
    kept = public_obs()
    # Frames a person marked surplus. They are not published — that is the point —
    # but the occurrence payload is built from `kept + surplus` so a find can still
    # say how many photographs stand behind it. Counting only what is carried would
    # make curation look like a thinner survey.
    surplus = surplus_records(load(OBS_F, []))
    from collections import Counter
    held = Counter(r for r in (withheld_reason(o) for o in load(OBS_F, [])) if r)
    withheld = len(load(LOCAL_OBS_F, []))
    # Images either ride along in public/ or come from R2. The public base URL is
    # not a secret and lives in a tracked config, so the deploy runner can build
    # correct URLs without ever holding credentials — uploads happen locally.
    cfg = load(PUBCFG_F, {})
    base = (cfg.get("r2_public_base") or "").rstrip("/")
    prefix = (cfg.get("r2_prefix") or "thumbs").strip("/")
    for o in kept:
        o.pop("hash", None)
        o["thumb"] = f"{base}/{prefix}/{o['file']}" if base else f"thumbs/{o['file']}"
    species = recorded_species(enriched_species(), kept)
    payload = {"species": species, "observations": kept,
               # Built from the PUBLISHED set: a withheld photograph is not evidence
               # anyone can act on, so it must not appear in a call for help either.
               # Surplus frames are the one exception — they are real evidence of
               # the find, just not carried as images, so they still count towards it.
               "occurrences": occurrence_payload(kept + surplus),
               "generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}
    if cfg.get("notice"):
        payload["notice"] = cfg["notice"]
    if cfg.get("contributor_endpoint"):
        payload["contributor_endpoint"] = cfg["contributor_endpoint"]
    (pub / "app" / "data.js").write_text("window.PLANT_DB = " + json.dumps(payload, indent=1) + ";\n")

    n = 0
    if base:
        n = upload_thumbs(kept, prefix, args)
    else:
        # Copy only the thumbnails the published data actually references — that way
        # a rejected (or deleted) observation can't leave an orphan image behind.
        (pub / "thumbs").mkdir(parents=True, exist_ok=True)
        for o in kept:
            t = THUMBS / o["file"]
            if t.exists():
                shutil.copy2(t, pub / "thumbs" / t.name)
                n += 1
    (pub / ".nojekyll").touch()
    size = sum(f.stat().st_size for f in pub.rglob("*") if f.is_file()) / 1e6
    # `n` from the R2 path is everything ever uploaded, which is not the same as
    # what this site references — a record that stops being published leaves its
    # object behind. Report what the site actually uses, and name the difference.
    shown = len(kept) if base else n
    where = f"{shown} thumbnail(s) on R2" if base else f"{n} thumbnails bundled"
    print(f"Built public/ — {len(payload['species'])} species, {where}, {size:.1f} MB.")
    if base:
        orphans = sorted(set(load(R2_MANIFEST, {})) - {o["file"] for o in kept})
        if orphans:
            print(f"{len(orphans)} object(s) on R2 are no longer referenced by the site. "
                  "They stay publicly reachable by URL until deleted:")
            print("  python3 scripts/plantdb.py publish --prune-r2")
        if orphans and getattr(args, "prune_r2", False):
            prune_r2(orphans, prefix)
    if held:
        print(f"Withheld {sum(held.values())} photo(s) — kept in the data, not published:")
        for reason, n in held.most_common():
            print(f"  {n:>3}  {reason}")
    if withheld:
        print(f"Withheld {withheld} local-only record(s) from {LOCAL_OBS_F.name} — not published.")
    print("Full-resolution originals are never published — they stay in the "
          "contributor\ninbox bucket or in Drive, depending how they arrived.")


def prune_r2(orphans, prefix):
    """Delete R2 objects the published site no longer references.

    Withholding a record hides it from the site but leaves its image hosted, still
    reachable by anyone with the URL. Local thumbnails are untouched, so a record
    that becomes publishable again just re-uploads on the next publish — this is
    reversible, which is why it does not ask twice.
    """
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import r2
    creds = r2.config()
    if not creds:
        print("  ! R2 credentials not set — cannot prune. Nothing deleted.")
        return
    manifest, gone = load(R2_MANIFEST, {}), 0
    for name in orphans:
        try:
            r2.delete(creds, f"{prefix}/{name}")
            manifest.pop(name, None)
            gone += 1
        except Exception as e:
            print(f"  ! could not delete {name}: {e}")
    save(R2_MANIFEST, manifest)
    print(f"Pruned {gone} unreferenced object(s) from R2. "
          "Local thumbnails kept — they re-upload if a record publishes again.")


def upload_thumbs(kept, prefix, args):
    """Push any thumbnail R2 doesn't already have. Returns the number now hosted.

    A manifest of what's been uploaded (keyed by content hash) keeps this cheap on
    re-runs — at survey scale, re-uploading thousands of unchanged images every
    publish would dominate the run.
    """
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import r2

    have = load(R2_MANIFEST, {})
    todo, lost = [], []
    for o in kept:
        t = THUMBS / o["file"]
        if not t.exists():
            # No local copy. Fine if it is already hosted — that is the normal case
            # on a fresh clone and on any runner that did not ingest this photo. Not
            # fine otherwise: the site would go out pointing at an image that exists
            # nowhere. Silently skipping this is how you ship a page of broken
            # thumbnails and only find out by looking.
            if o["file"] not in have:
                lost.append(o["file"])
            continue
        h = sha(t)
        if have.get(o["file"]) != h:
            todo.append((o["file"], t, h))

    if lost:
        print(f"  ! {len(lost)} publishable record(s) have no thumbnail locally and none "
              "on R2:")
        for name in lost[:8]:
            print(f"      {name}")
        if len(lost) > 8:
            print(f"      ... and {len(lost) - 8} more")
        print("    Their images exist nowhere. Re-ingest them, then publish again:")
        print("      python3 scripts/plantdb.py ingest-drive")
        sys.exit(1)

    if getattr(args, "no_upload", False):
        if todo:
            print(f"  ! {len(todo)} thumbnail(s) not on R2 and --no-upload was set — "
                  "the published site will have broken images until you upload them.")
        return len(have)

    if not todo:
        return len(have)

    cfg = r2.config()
    bucket = cfg["R2_BUCKET"] if cfg else load(PUBCFG_F, {}).get("r2_bucket", "")
    via_wrangler = not cfg and bucket and shutil.which("npx")

    if not cfg and not via_wrangler:
        # CI has no credentials by design; it only rebuilds HTML and URLs.
        print(f"  ! {len(todo)} thumbnail(s) need uploading but R2 credentials are not set "
              f"({', '.join(r2.missing_vars())}).")
        print("    Run publish locally to upload them, or pass --no-upload to acknowledge.")
        sys.exit(1)

    how = "S3 API" if cfg else "wrangler (slower — one process per file)"
    print(f"Uploading {len(todo)} new thumbnail(s) to R2 via {how}...")
    done = 0
    for name, path, h in todo:
        key = f"{prefix}/{name}"
        try:
            if cfg:
                r2.put(cfg, key, path.read_bytes())
            else:
                r = subprocess.run(
                    ["npx", "wrangler", "r2", "object", "put", f"{bucket}/{key}",
                     "--file", str(path), "--content-type", "image/jpeg", "--remote"],
                    capture_output=True, text=True, timeout=120)
                if r.returncode != 0:
                    raise r2.R2Error(f"{key}: wrangler exited {r.returncode} — "
                                     f"{(r.stderr or r.stdout).strip()[:200]}")
        except Exception as e:
            save(R2_MANIFEST, have)   # keep what did succeed
            sys.exit(f"Upload failed: {e}\nNothing published — fix this and re-run.")
        have[name] = h
        done += 1
        if done % 10 == 0 or done == len(todo):
            print(f"  {done}/{len(todo)}")
    save(R2_MANIFEST, have)
    return len(have)


def cmd_refresh_gps(args):
    """Re-read coordinates and capture time from the originals in photos/.

    Needed for anything ingested before EXIF was parsed directly, and useful any
    time a record has lost its location. Only fills blanks unless --force.
    """
    obs = load_obs()
    fixed, missing = 0, 0
    for o in obs:
        if o.get("lat") and not args.force:
            continue
        src = PHOTOS / o["file"]
        if not src.exists():
            missing += 1
            continue
        e = exif_of(src)
        if e["lat"]:
            o["lat"], o["lon"] = blur(e["lat"]), blur(e["lon"])
            if e["taken"]:
                o["taken"] = e["taken"]
            fixed += 1
    save_obs(obs)
    cmd_build(args)
    print(f"\nRecovered coordinates for {fixed} record(s).")
    if missing:
        print(f"{missing} record(s) have no original in photos/ — nothing to re-read.")
    still = sum(1 for o in load_obs() if not o.get("lat"))
    if still:
        print(f"{still} record(s) still have no location — their originals carry no GPS.")


def cmd_confirm(args):
    """Record that a person checked a record in the field.

    This is the step the whole project turns on — until it happens a record is a
    lead, not a finding. Deliberately one record at a time and never inferred:
    nothing else in the pipeline may write these fields.
    """
    obs = load_obs()
    want = set(args.file or [])
    sel = [o for o in obs if o["file"] in want or o["id"] in want]
    missing = want - {o["file"] for o in sel} - {o["id"] for o in sel}
    if missing:
        sys.exit(f"No record matches: {', '.join(sorted(missing))}")
    if not sel:
        sys.exit("Pass --file with one or more filenames or record ids.")

    if args.status == "corrected" and not args.species:
        sys.exit("--status corrected needs --species <id> (what it actually is).")
    ids = {s["id"] for s in load(SPECIES_F, [])}
    if args.species and args.species not in ids:
        sys.exit(f"Unknown species id '{args.species}'. See: plantdb.py species")

    for o in sel:
        v = {"status": args.status, "by": args.by,
             "date": args.date or datetime.date.today().isoformat(), "via": "cli"}
        if args.species:
            v["species_id"] = args.species
        if args.notes:
            v["notes"] = args.notes
        o["verified"] = v
        was = o.get("species_id", "unknown")
        now = effective_species(o)
        change = f"  {was} -> {now}" if now != was else ""
        print(f"  {o['file'][:14]}…  {args.status} by {args.by}{change}")
    save_obs(obs)
    cmd_build(args)
    print(f"\nRecorded {len(sel)} field verification(s).")


def cmd_unverified(args):
    """What still needs a person to go and look, most urgent first."""
    # One line per find. A patch photographed seven times is one walk, not seven,
    # and listing it seven times buries the other things that need checking.
    obs = [o for o in load_obs() if not is_verified(o)]
    species = {s["id"]: s for s in enriched_species()}
    rows = []
    for o, n, _ in one_per_patch(obs):
        sp = species.get(effective_species(o), {})
        rows.append((RANK.get(sp.get("origin_status", "unknown"), 9), o, sp, n))
    rows.sort(key=lambda r: (r[0], r[1].get("taken", "")))
    if args.status:
        rows = [r for r in rows if r[2].get("origin_status") == args.status]
    if not rows:
        print("Everything is field-verified.")
        return
    print(f"{len(rows)} record(s) awaiting field verification:\n")
    cur = None
    for rank, o, sp, n in rows[: args.limit or len(rows)]:
        st = sp.get("origin_status", "unknown")
        if st != cur:
            cur = st
            print(f"\n=== {st.upper()} ===")
        print(f"  {o['file']}")
        print(f"    {sp.get('common', o.get('species_id'))}  [{o.get('confidence','?')}]"
              f"  {o.get('lat','')}, {o.get('lon','')}"
              + (f"  · {n} photographs of this patch" if n > 1 else ""))
    print("\nTo record a check:")
    print("  python3 scripts/plantdb.py confirm --file <name> --by \"Your Name\" --status confirmed")


def require(module, pip_name):
    """Import a third-party module, re-running under .venv if that is where it lives.

    Most of this tool is stdlib-only and runs fine under system python, so the docs
    say `python3 scripts/plantdb.py` everywhere. Two commands need packages that are
    only in .venv, and the difference is invisible until it fails. Rather than make
    every command carry a venv prefix it does not need, re-exec the ones that do.
    """
    import importlib
    try:
        return importlib.import_module(module)
    except ImportError:
        pass
    venv = ROOT / ".venv" / "bin" / "python"
    # Compare interpreter *prefixes*, never resolved paths: .venv/bin/python is a
    # symlink to the base interpreter, so resolving both sides makes an outside
    # python look like it is already inside the venv. The venv also only works when
    # invoked through the symlink — resolving the path away loses its site-packages.
    in_venv = pathlib.Path(sys.prefix) == (ROOT / ".venv")
    if venv.exists() and not in_venv:
        have = subprocess.run([str(venv), "-c", f"import {module}"], capture_output=True)
        if have.returncode == 0:
            os.execv(str(venv), [str(venv)] + sys.argv)   # replaces this process
    sys.exit(f"This command needs the '{pip_name}' package. Install it with:\n"
             f"  .venv/bin/pip install {pip_name}")



def _drive():
    require("googleapiclient", "google-api-python-client google-auth")
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import drive
    cfg = drive.config()
    if not cfg:
        sys.exit("Drive not configured. Missing: " + ", ".join(drive.missing_vars()))
    return drive, cfg


def cmd_drive_folders(args):
    """List folders the service account can see — this is how you find the folder id."""
    drive, cfg = _drive()
    svc = drive.service(cfg)
    folders = drive.shared_folders(svc)
    if not folders:
        print("No folders shared with the service account yet.")
        print("Share the photo folder with the address in your service-account JSON")
        print("(the `client_email` field), as Viewer or better.")
        return
    print(f"{len(folders)} folder(s) visible to the service account:\n")
    for f in folders:
        owner = (f.get("owners") or [{}])[0].get("emailAddress", "?")
        mark = "  <- GOOGLE_DRIVE_FOLDER_ID" if f["id"] == cfg["folder_id"] else ""
        print(f"  {f['name']}")
        print(f"    id: {f['id']}   owner: {owner}{mark}")
    if not cfg["folder_id"]:
        print("\nSet GOOGLE_DRIVE_FOLDER_ID in .env to the id you want to watch.")


def cmd_ingest_drive(args):
    """Fetch new photos from the shared Drive folder, then ingest them normally."""
    drive, cfg = _drive()
    if not cfg["folder_id"]:
        sys.exit("Set GOOGLE_DRIVE_FOLDER_ID in .env. To find it: plantdb.py drive-folders")
    svc = drive.service(cfg)
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import idcache
    con = idcache.connect()
    seen = idcache.drive_seen(con)

    files = drive.list_images(svc, cfg["folder_id"])
    new = [f for f in files if f["id"] not in seen]
    print(f"{len(files)} image(s) in the folder, {len(new)} not yet fetched.")
    if args.limit:
        new = new[: args.limit]
    if not new:
        return

    staging = ROOT / ".drive-inbox"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()
    got, fetched = 0, []
    for f in new:
        dest = staging / f["name"]
        n = 1
        while dest.exists():                      # Drive allows duplicate names
            dest = staging / f"{pathlib.Path(f['name']).stem}-{n}{pathlib.Path(f['name']).suffix}"
            n += 1
        try:
            drive.download(svc, f["id"], dest)
        except Exception as e:
            print(f"  ! {f['name']}: download failed — {e}")
            continue
        fetched.append(f)
        got += 1
        print(f"  + {f['name']}")
    print(f"\nDownloaded {got} file(s) to {staging.name}/")
    if got:
        args.folder = str(staging)
        args.batch = args.batch or datetime.date.today().isoformat()
        cmd_ingest(args)
    # Marked as fetched only once ingest has actually run, and only for files that
    # reached the library. Recording them at download time meant a crash in between
    # — or a photo ingest skipped, or the staging directory wiped at the start of
    # the next run — left the id marked seen with nothing to show for it, and that
    # photo was never fetched again. Re-downloading costs bandwidth; losing a
    # contributor's photograph costs the survey a record it cannot get back.
    in_library = {o.get("hash") for o in load_obs()}
    for f in fetched:
        p = staging / f["name"]
        if not p.exists() or sha(p) in in_library:
            idcache.drive_record(con, f["id"], f["name"])
        else:
            print(f"  ! {f['name']} did not make it into the library — will retry next run")
    shutil.rmtree(staging, ignore_errors=True)


def verification_problem(v, species_ids):
    """Why these human columns are not an acceptable verification, or None.

    One definition, shared by everything that accepts or reports on a verification:
    the site offers it, the Worker refuses early with it, `inbox-pull` decides what
    to apply with it, and the receipt a contributor reads quotes it. If any two of
    those disagreed, somebody would be told their field check had landed while the
    pipeline quietly dropped it.
    """
    status = (v.get("status") or "").strip()
    if not status:
        return None                     # blank is "not reviewed", not an error
    if status not in VERIFY_STATUS:
        return f"status '{status}' is not one of {', '.join(VERIFY_STATUS)}"
    if status == "corrected" and not v.get("species_id"):
        return "'corrected' needs an id in 'corrected species'"
    if v.get("species_id") and v["species_id"] not in species_ids:
        return f"unknown species id '{v['species_id']}' — pick one from the Species tab"
    if not v.get("by"):
        return "needs a name in 'verified by' — an unattributed verification is not one"
    return None




# What an `edit` from the site may touch, and what it may never touch.
#
# The machine's answer is not editable. `species_id`, `note` and `confidence` stay
# exactly as identification left them however wrong they are, because the survey's
# whole claim rests on being able to show what the model said next to what a person
# found — and because `export-imap` names an Observer only where a human verdict
# exists. A correction is a `verified` record with status `corrected`, not an
# overwrite, so a human is never able to quietly become the source of a machine
# identification.
#
# What IS editable is the stuff a person can genuinely know better than the file
# did: where it was and when. Both keep their original alongside, because a
# coordinate somebody typed and a coordinate a camera recorded are different kinds
# of claim and the survey has to be able to tell a reader which it is holding.
EDITABLE = ("lat", "lon", "taken", "curator_note")


def edit_problem(e, o):
    """Why this edit will not be applied, or None."""
    touched = [k for k in e if k in ("lat", "lon", "taken", "curator_note")]
    refused = [k for k in e if k in ("species_id", "note", "confidence", "also", "rejected")]
    if refused:
        return (f"{', '.join(refused)} is the identification's own answer and is never "
                "overwritten — record a correction instead, which keeps both")
    if not touched:
        return f"nothing to change (editable: {', '.join(EDITABLE)})"
    if ("lat" in e) != ("lon" in e):
        return "a coordinate has to be changed as a pair, or the record lands somewhere neither"
    for k in ("lat", "lon"):
        if k in e:
            try:
                float(e[k])
            except (TypeError, ValueError):
                return f"{k} is not a number"
    if "lat" in e and not (-90 <= float(e["lat"]) <= 90):
        return "latitude is outside -90..90"
    if "lon" in e and not (-180 <= float(e["lon"]) <= 180):
        return "longitude is outside -180..180"
    if "taken" in e and e["taken"] and not re.match(r"^\d{4}-\d{2}-\d{2}", str(e["taken"])):
        return "a capture date has to start yyyy-mm-dd"
    return None


def _sub_problem(s, obs_by_file, species_ids):
    """Why this submission cannot be applied, or None. Never raises on bad input —
    the payload came off the network and may be anything at all."""
    kind, role = s.get("kind"), s.get("role")
    # Which capability each kind needs. Withdrawing, restoring, editing and adding
    # a catalogue entry are all curation, so they all sit behind `redundant` — the
    # admin grant — rather than each inventing its own.
    NEEDS = {"verify": "verify", "redundant": "redundant", "upload": "upload",
             "edit": "redundant", "withdraw": "redundant", "restore": "redundant",
             "species": "redundant"}
    if kind not in NEEDS:
        return f"unknown submission kind '{kind}'"
    if not may(role, NEEDS[kind]):
        return (f"a '{role}' may not {kind} — this submission was accepted by the "
                f"Worker but is refused here")
    if not s.get("by"):
        return "no contributor name on the submission"

    if kind == "upload":
        return None                      # the ingest path does its own checking

    if kind == "species":
        sid = (s.get("species_id") or "").strip()
        if not re.fullmatch(r"[a-z0-9-]{2,60}", sid):
            return "a species id is lower-case letters, digits and hyphens"
        if sid in species_ids:
            return f"'{sid}' is already in the catalogue"
        if not (s.get("common") or "").strip():
            return "a catalogue entry needs a common name"
        return None

    o = obs_by_file.get(s.get("file"))
    if o is None:
        return f"no record named '{s.get('file')}'"

    if kind == "verify":
        if not s.get("status"):
            return None                  # a withdrawal; nothing more to check
        return verification_problem(
            {"status": s.get("status"), "species_id": s.get("species_id"),
             "by": s.get("by")}, species_ids)

    if kind == "withdraw":
        if is_withdrawn(o):
            return "that record is already withdrawn"
        return withdrawal_problem({"by": s["by"], "reason": s.get("reason")})

    if kind == "restore":
        return None if is_withdrawn(o) else "that record is not withdrawn"

    if kind == "edit":
        return edit_problem(s.get("changes") or {}, o)

    r = {"by": s["by"], "of": s.get("of"), "species_id": s.get("species_id")}
    if s.get("unmark"):
        return None if is_redundant(o) else "that photograph is not marked surplus"
    return redundancy_problem(r, o, obs_by_file.get(s.get("of")), species_ids,
                              list(obs_by_file.values()))


def cmd_inbox_pull(args):
    """Apply what contributors submitted through the site. Previews by default.

    Deliberately the same shape as `sheet-pull`, down to the withdrawal guard:
    both are unattended drains of a queue a person edits, and someone reading one
    should already understand the other.
    """
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import inbox
    cfg = inbox.config()
    if not cfg:
        # Not an error. Contributor mode is optional, the pipeline calls this on
        # every tick, and a survey with no Worker has to keep running untouched.
        print("Contributor mode is not configured — nothing to collect. "
              f"(Set {', '.join(inbox.missing_vars())} to enable it.)")
        return

    try:
        subs = inbox.pending(cfg)
    except inbox.InboxError as e:
        # A Worker that is down must not fail the nightly run: identifications are
        # already paid for and the rest of the tick still has work to do.
        print(f"  ! could not reach the contributor inbox: {e}")
        return

    obs = load_obs()
    by_file = {o["file"]: o for o in obs}
    ids = {s["id"] for s in load(SPECIES_F, [])}

    applied, refused, uploads = [], [], []
    for s in subs:
        if (problem := _sub_problem(s, by_file, ids)):
            refused.append((s, problem))
            continue
        if s["kind"] == "upload":
            uploads.append(s)
        else:
            applied.append(s)

    if not subs:
        print("Nothing waiting in the contributor inbox.")
        return

    # Same reasoning as sheet-pull, and the same limit. A field app makes a
    # withdrawal one tap, so if anything this matters more here.
    clears = [s for s in applied if s["kind"] == "verify" and not s.get("status")
              and (by_file[s["file"]].get("verified"))]
    if len(clears) > MAX_UNATTENDED_CLEARS and not args.force:
        print(f"\n  ! {len(clears)} verification(s) would be WITHDRAWN in one collection:")
        for s in clears[:10]:
            print(f"      {s['file'][:14]}…  by {s['by']}")
        print(f"\n    More than {MAX_UNATTENDED_CLEARS} at once is more likely a mistake than"
              "\n    that many people changing their mind. Nothing was applied — not even"
              "\n    the other submissions. Re-run with --force if it is genuinely right.")
        sys.exit(1)

    print(f"{len(subs)} submission(s) in the contributor inbox:")
    for s in applied:
        what = s.get("file") or s.get("species_id") or ""
        print(f"  {s['kind']:<9} {what[:14]:<15} by {s['by']} ({s['role']})")
    for s in uploads:
        print(f"  upload    {s.get('filename','')[:14]:<15} by {s['by']} ({s['role']})")
    for s, why in refused:
        print(f"  ! refused  {(s.get('file') or s.get('filename') or '')[:14]:<15} {why}")
    if not args.yes:
        print("\nNothing changed. Re-run with --yes to apply.")
        return

    n_new, upload_refusals = 0, []
    if uploads:
        n_new, upload_refusals = _apply_uploads(cfg, inbox, uploads, obs, by_file)
        refused += upload_refusals
        uploads = [s for s in uploads if s not in [r[0] for r in upload_refusals]]
    new_species = [s for s in applied if s["kind"] == "species"]
    if new_species:
        _apply_new_species(new_species)
    for s in applied:
        if s["kind"] != "species":
            _apply_submission(s, by_file[s["file"]])
    if applied or n_new:
        obs.sort(key=lambda o: o.get("taken", ""))
        save_obs(obs)
        cmd_build(args)

    # Receipts last, and only for what actually landed. The site reads these back
    # so a contributor sees "recorded" or the reason it was not — the inbox's
    # version of the sheet's `recorded?` column, and it exists for the same
    # reason: a refusal that only appears in a CI log leaves the person who walked
    # out there believing it was recorded.
    for s in applied + uploads:
        _receipt(inbox, cfg, s["id"], "recorded", "")
    for s, why in refused:
        _receipt(inbox, cfg, s["id"], "refused", why)

    print(f"\nApplied {len(applied)} submission(s) and {n_new} new photograph(s); "
          f"refused {len(refused)}.")


def _receipt(inbox, cfg, sub_id, status, detail):
    """Acknowledge one submission. A failure here must not undo applied work."""
    try:
        inbox.receipt(cfg, sub_id, status, detail)
    except inbox.InboxError as e:
        print(f"  ! could not acknowledge {sub_id}: {e} (it will be offered again)")


def _apply_new_species(subs):
    """Add catalogue entries somebody created on the site.

    Marked `source: "hand (site)"` rather than `auto (...)`, which is not
    cosmetic: `reconcile` only ever drops machine-created entries, precisely so
    an unattended run cannot delete an editorial decision. An entry a person
    wrote in order to correct a record to it must survive the next reconcile.
    """
    species = load(SPECIES_F, [])
    for s in subs:
        species.append({
            "id": s["species_id"].strip(),
            "common": s["common"].strip(),
            "scientific": (s.get("scientific") or "").strip(),
            "family": (s.get("family") or "").strip(),
            "kind": (s.get("kind") or "herb").strip(),
            "summary": (s.get("summary") or "").strip(),
            "origin_status": (s.get("origin_status") or "unknown"),
            "source": f"hand (site) — {s['by']}",
        })
        print(f"  + catalogue entry '{s['species_id']}' ({s['common']}) by {s['by']}")
    save(SPECIES_F, species)


def _apply_submission(s, o):
    """Write one submission onto its record."""
    today = datetime.date.today().isoformat()

    if s["kind"] == "withdraw":
        o["withdrawn"] = {"by": s["by"], "date": s.get("date") or today,
                          "reason": s["reason"]}
        return
    if s["kind"] == "restore":
        o.pop("withdrawn", None)
        return
    if s["kind"] == "edit":
        changes = s.get("changes") or {}
        # Coordinates and dates keep what they replaced. A coordinate somebody
        # typed and a coordinate a camera recorded are different kinds of claim,
        # and a survey whose deliverable is location has to be able to say which
        # one it is holding — and to give the original back if the correction was
        # itself wrong.
        if "lat" in changes and "lon" in changes:
            if o.get("lat") and "location_was" not in o:
                o["location_was"] = {"lat": o["lat"], "lon": o["lon"],
                                     "source": o.get("location_source", "exif")}
            o["lat"], o["lon"] = blur(changes["lat"]), blur(changes["lon"])
            o["location_source"] = "corrected-by-hand"
            o["location_corrected_by"] = s["by"]
        if "taken" in changes:
            if o.get("taken") and "taken_was" not in o:
                o["taken_was"] = o["taken"]
            o["taken"] = changes["taken"]
        if "curator_note" in changes:
            # Additive annotation. It sits BESIDE the model's `note`, which is
            # never touched, so the page can show both and a reader can tell a
            # machine's description from a person's.
            text = (changes["curator_note"] or "").strip()
            if text:
                o["curator_note"] = {"text": text, "by": s["by"],
                                     "date": s.get("date") or today}
            else:
                o.pop("curator_note", None)
        return

    if s["kind"] == "verify":
        if not s.get("status"):
            o.pop("verified", None)
            return
        v = {"status": s["status"], "by": s["by"],
             "date": s.get("date") or datetime.date.today().isoformat(), "via": "site"}
        if s.get("species_id"):
            v["species_id"] = s["species_id"]
        if s.get("notes"):
            v["notes"] = s["notes"]
        o["verified"] = v
        return
    if s.get("unmark"):
        o.pop("redundant", None)
        return
    r = {"by": s["by"], "date": s.get("date") or datetime.date.today().isoformat(),
         "of": s["of"], "species_id": s["species_id"]}
    if s.get("notes"):
        r["notes"] = s["notes"]
    o["redundant"] = r


def _apply_uploads(cfg, inbox, uploads, obs, by_file):
    """Fetch uploaded photographs and put them through the ordinary ingest path.

    Nothing here shortcuts screening, identification or the content-hash dedupe.
    A photograph uploaded from a phone is a photograph.

    THE LOCATION RULE, which is the whole of the difference:

    A record with no location or no date is not a survey record — it cannot be
    checked and cannot be compared against a later visit — so one is never
    accepted. There are exactly two ways an upload gets a location, and neither
    is a guess:

      * the photograph carries its own, read from EXIF by `exif_of`; or
      * it was attached to a find that already exists, and inherits THAT find's
        coordinates, because the person uploading it said this is another
        photograph of that patch.

    The browser refuses most of these before a byte is sent, which is the low
    friction part. This is the check that actually decides, because it is the one
    running where the authoritative EXIF reader is.
    """
    known = {o.get("hash") for o in obs if o.get("hash")}
    tmp = ROOT / ".inbox-uploads"
    tmp.mkdir(exist_ok=True)
    added, rejected = 0, []
    try:
        for s in uploads:
            try:
                data = inbox.photo(cfg, s["id"])
            except inbox.InboxError as e:
                print(f"  ! {s.get('filename','?')}: could not fetch the image ({e})")
                continue
            name = re.sub(r"[^A-Za-z0-9._-]", "_", s.get("filename") or f"{s['id']}.jpg")
            src = tmp / name
            src.write_bytes(data)

            # Inheriting the find's coordinates. Only from a record that exists and
            # is actually located — never from whatever the browser claimed the
            # find was, or a bad `attach_to` would place a photograph anywhere.
            inherit = {}
            anchor = by_file.get(s.get("attach_to") or "")
            if anchor and _located(anchor):
                inherit = {"lat": anchor["lat"], "lon": anchor["lon"],
                           "source": "attached-to-find", "of": anchor["file"]}

            rec = ingest_file(
                src, s.get("walk") or s.get("batch") or "field-app", obs, known,
                extra={"submitted_by": s["by"], "submitted": s.get("submitted", ""),
                       # Where the original lives now and permanently. photos/ is
                       # emptied with the runner; this is the archive that is not.
                       "archive_key": f"originals/{s['id']}",
                       "walk": s.get("walk") or ""},
                fallback={"lat": inherit.get("lat"), "lon": inherit.get("lon"),
                          "taken": (s.get("taken") or "")[:19],
                          "source": inherit.get("source"), "of": inherit.get("of")})
            if rec is None:
                print(f"  - {name}: already in the library, or could not be read")
                continue

            # The gate. A photograph that reached here without a location or a date
            # is not a survey record and never becomes one, so the record is undone
            # rather than left to sit withheld forever with nobody told why.
            missing = [w for w, ok in (("location", rec.get("lat") and rec.get("lon")),
                                       ("capture date", rec.get("taken"))) if not ok]
            if missing:
                obs.remove(rec)
                known.discard(rec["hash"])
                (THUMBS / rec["file"]).unlink(missing_ok=True)
                (PHOTOS / rec["file"]).unlink(missing_ok=True)
                rejected.append((s, f"the photograph has no {' and no '.join(missing)}. "
                                    "Re-export the original with its metadata intact, or "
                                    "attach it to a find that already exists."))
                print(f"  ! {name}: refused — no {' and no '.join(missing)}")
                continue

            if rec.get("location_source") == "attached-to-find":
                print(f"    location inherited from {inherit['of'][:14]}… (attached to that find)")
            added += 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return added, rejected


def cmd_contributor(args):
    """Mint, list and revoke the tokens that let a person write to the survey.

    A token carries the contributor's NAME and ROLE, and the Worker reads both
    from it rather than from anything the browser sends. That is what makes
    attribution structural: `verified.by` cannot be blank, cannot be someone
    else, and cannot be typed into a box, because the browser never gets a say in
    it. The sheet has to refuse unattributed verifications after the fact; here
    one cannot be constructed.

    Only the hash of a token is ever stored, so a leaked database does not let
    anyone write, and the plaintext is shown exactly once at minting.
    """
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import inbox as inbox_mod
    import secrets

    if args.action == "bootstrap":
        # The first admin exists before any admin API can be called, so it is
        # inserted straight into D1. Printed rather than run: this needs wrangler
        # and a person's Cloudflare login, and it is a one-time step.
        token = "sif_" + secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        print("Run this once, then keep the token somewhere safe — it is not recoverable:\n")
        print(f"  npx wrangler d1 execute sears-island-contributors --remote --command \\\n"
              f"    \"INSERT INTO contributors (id, name, role, token_sha256, created) "
              f"VALUES (lower(hex(randomblob(8))), '{args.name}', 'admin', "
              f"'{digest}', datetime('now'));\"\n")
        print(f"  {token}\n")
        print("Then put it in .env as SIF_ADMIN_TOKEN, and mint everyone else with")
        print("  python3 scripts/plantdb.py contributor add --name \"…\" --role verifier")
        return

    url = os.environ.get("SIF_WORKER_URL", "").strip().rstrip("/")
    token = os.environ.get("SIF_ADMIN_TOKEN", "").strip()
    if not (url and token):
        sys.exit("Set SIF_WORKER_URL and SIF_ADMIN_TOKEN in .env first "
                 "(`contributor bootstrap` mints the first admin token).")
    cfg = {"url": url, "token": token}

    try:
        if args.action == "list":
            for c in inbox_mod._call(cfg, "GET", "/api/contributors").get("contributors", []):
                state = "" if c.get("active") else "   (revoked)"
                seen = c.get("last_seen") or "never used"
                print(f"  {c['id']}  {c['role']:<12} {c['name']:<24} last seen {seen}{state}")
            return
        if args.action == "add":
            if args.role not in ROLES:
                sys.exit(f"--role must be one of {', '.join(ROLES)}")
            got = inbox_mod._call(cfg, "POST", "/api/contributors",
                                  {"name": args.name, "role": args.role})
            print(f"Minted a '{args.role}' token for {args.name}.\n")
            print(f"  {url}/#key={got['token']}\n")
            print("Send them that link. Opening it once signs them in on that device and")
            print("the token is not shown again — mint a new one and revoke this if it is lost.")
            if args.role == "pipeline":
                print("\nThis is the unattended drain token: put it in .env as "
                      "SIF_PIPELINE_TOKEN\nand in the repository's Actions secrets. It can "
                      "collect the inbox and nothing else.")
            return
        inbox_mod._call(cfg, "POST", f"/api/contributors/{args.id}/revoke", {})
        print(f"Revoked {args.id}. Anything they already recorded stays — it was still "
              "a person\nwho went and looked.")
    except inbox_mod.InboxError as e:
        sys.exit(f"{e}")


def cmd_redundant(args):
    """Mark a photograph surplus to another one of the same find, from a terminal.

    The site is where this normally happens, but the survey should never have a
    capability that only exists behind a deployed Worker — that is how a project
    ends up unable to fix its own data when something is down.
    """
    obs = load_obs()
    by_file = {o["file"]: o for o in obs}
    ids = {s["id"] for s in load(SPECIES_F, [])}
    o = by_file.get(args.file)
    if o is None:
        sys.exit(f"No record named '{args.file}'.")
    if args.unmark:
        if not is_redundant(o):
            sys.exit(f"{args.file} is not marked surplus.")
        who = o["redundant"]["by"]
        o.pop("redundant")
        save_obs(obs)
        cmd_build(args)
        print(f"Unmarked {args.file} (was marked by {who}). It is carried again "
              "from the next publish.")
        return

    rep = by_file.get(args.of)
    r = {"by": args.by, "of": args.of,
         "species_id": args.species or (rep and effective_species(rep))}
    if (why := redundancy_problem(r, o, rep, ids, obs)):
        sys.exit(f"Refused: {why}")
    r["date"] = args.date or datetime.date.today().isoformat()
    if args.notes:
        r["notes"] = args.notes
    o["redundant"] = r
    save_obs(obs)
    cmd_build(args)
    print(f"{args.file} is surplus to {args.of} for {r['species_id']}, marked by {args.by}.")
    where = archive_of(o) or "wherever its original came from"
    print(f"The record and its original are untouched \u2014 the original is in {where}.")
    print("Only the published image stops being carried, from the next publish.")


def cmd_withdraw(args):
    """Take a record off the site, or put it back, from a terminal.

    The site does this too, but a withdrawn record is not in the published data —
    that is the point of withdrawing it — so the site can only offer to restore
    one while it is still carried. This is the way back for anything already gone,
    and the reason the withdrawal is stored rather than the record deleted.
    """
    obs = load_obs()
    o = next((x for x in obs if x["file"] == args.file or x["id"] == args.file), None)
    if o is None:
        sys.exit(f"No record named '{args.file}'.")
    if args.restore:
        if not is_withdrawn(o):
            sys.exit(f"{o['file']} is not withdrawn.")
        was = o.pop("withdrawn")
        print(f"Restored {o['file']} (withdrawn by {was['by']}: {was.get('reason','')}).")
    else:
        w = {"by": args.by, "reason": args.reason or "",
             "date": args.date or datetime.date.today().isoformat()}
        if (why := withdrawal_problem(w)):
            sys.exit(f"Refused: {why}")
        o["withdrawn"] = w
        where = archive_of(o) or "wherever its original came from"
        print(f"Withdrew {o['file']} — {w['reason']}")
        print(f"The record is kept and the original is in {where}. Put it back with:")
        print(f"  python3 scripts/plantdb.py withdraw --file {o['file']} --restore")
    save_obs(obs)
    cmd_build(args)


def cmd_withheld(args):
    """Everything the site is not carrying, and why.

    Withdrawn and surplus records are invisible on the published site by design,
    which makes them hard to find again. This is where they are.
    """
    obs = load_obs()
    rows = [(withheld_reason(o), o) for o in obs]
    rows = [(r, o) for r, o in rows if r]
    if not rows:
        print("Everything is carried.")
        return
    from collections import defaultdict
    groups = defaultdict(list)
    for r, o in rows:
        groups[r.split(" — ")[0]].append((r, o))
    print(f"{len(rows)} record(s) not carried on the site:\n")
    for head, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        print(f"=== {head} ({len(items)}) ===")
        for r, o in items[: args.limit or 100]:
            extra = r.split(" — ", 1)[1] if " — " in r else ""
            print(f"  {o['file']}{('  ' + extra) if extra else ''}")
        print()


def cmd_cache(args):
    """What we have already paid to identify, and what it cost."""
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import idcache
    con = idcache.connect()
    st = idcache.stats(con)
    if not st["count"]:
        print(f"No identifications cached yet ({idcache.DB.relative_to(ROOT)}).")
        return
    print(f"{st['count']} identification(s) cached, {st['first']} to {st['last']}")
    print(f"  tokens: {st['input_tokens']:,} in / {st['output_tokens']:,} out")
    print(f"  spent:  ${st['cost_usd']:.2f}")
    for model, n, cost in st["by_model"]:
        print(f"    {model or '?':<20} {n:>5} photo(s)  ${cost:.2f}")
    obs = load_obs()
    have = {h for (h,) in con.execute("SELECT hash FROM identifications")}
    uncached = [o for o in obs if o.get("hash") and o["hash"] not in have]
    print(f"\n{len(obs) - len(uncached)}/{len(obs)} record(s) covered by the cache.")
    if uncached:
        cin, cout = idcache.PRICES.get("claude-opus-5", (0, 0))
        est = len(uncached) * (4408 * cin + 750 * cout)
        print(f"{len(uncached)} would cost about ${est:.2f} to identify at current rates.")


# Columns whose names are taken from NatureServe's documented bulk-upload
# requirements. Everything after Longitude is a best guess at useful extra context
# and should be reconciled against the official template before a real submission —
# the required set is documented publicly, the optional set is not.
IMAP_REQUIRED = ["Source Unique ID", "Species", "Date", "Observer", "Latitude", "Longitude"]
IMAP_EXTRA = ["Common Name", "Comments", "Photo URL"]

# iMapInvasives is an INVASIVE species database with a jurisdiction-defined tracked
# species list. A native goldenrod record does not belong in it, however well
# verified — sending one is noise to the people receiving it.
IMAP_TRACKED = ("invasive", "regulated")


def imap_blocker(o, sp):
    """Why this record cannot go to iMapInvasives, or None if it can.

    The rule that does most of the work here is the project's own: nothing is a
    finding until a person has confirmed it on the ground. Submitting machine
    identifications into a state dataset that land managers act on would be a
    plain breach of that, and it would put unreviewed guesses somewhere they
    cannot easily be taken back.

    It also happens to solve the one field the survey does not otherwise collect.
    iMap requires an Observer — the person who observed the species — and for a
    field-verified record that is exactly who `verified.by` is. The constraint the
    project already imposes on itself supplies the field the state requires.
    """
    if o.get("rejected"):
        return "screened out — not a photograph of vegetation"
    v = o.get("verified") or {}
    if v.get("status") not in ("confirmed", "corrected"):
        return {"rejected": "a person looked and rejected the identification",
                "revisit": "flagged for another look — not settled"}.get(
                    v.get("status"), "not field-verified by a person")
    if not v.get("by", "").strip():
        return "no observer — the verification is unattributed"
    if not sp:
        return "species is not in the catalogue"
    if sp.get("origin_status") not in IMAP_TRACKED:
        return f"{sp.get('origin_status', 'unknown')} — iMapInvasives tracks invasives"
    if not (o.get("lat") and o.get("lon")):
        return "no coordinates"
    if not o.get("taken"):
        return "no observation date"
    return None


def cmd_export_imap(args):
    """Write field-verified invasive records as an iMapInvasives bulk-upload CSV.

    There is no public API to submit to — the bulk upload tool is run by the
    jurisdiction administrator, so this produces the file to hand them rather than
    posting anything anywhere.
    """
    import csv
    obs = [o for o in load_obs() if not o.get("local_only")]
    species = {s["id"]: s for s in enriched_species()}
    base = load(PUBCFG_F, {})
    url = (base.get("r2_public_base") or "").rstrip("/")
    prefix = (base.get("r2_prefix") or "thumbs").strip("/")

    # One row per OCCURRENCE, not per photograph. iMapInvasives records a species
    # observed at one location on one date, so seven photographs of one willowherb
    # patch are one record there — submitting seven would report seven infestations
    # to the state and overstate what is on the ground.
    rows, blocked = [], []
    for x in occurrences(obs):
        sp = species.get(x["species_id"])
        confirmed = next((o for o in x["members"]
                          if (o.get("verified") or {}).get("status") in ("confirmed", "corrected")),
                         None)
        why = imap_blocker(confirmed or x["anchor"], sp)
        if why:
            blocked.append((x, why))
            continue
        v = confirmed["verified"]
        notes = " ".join(t for t in (confirmed.get("note"), v.get("notes")) if t)
        if len(x["members"]) > 1:
            notes = (f"{len(x['members'])} photographs over ~{x['spread_m']:.0f} m, "
                     f"{x['first_seen']} to {x['last_seen']}. ") + notes
        rows.append({
            # The confirmed photograph's content hash: stable for the life of the
            # project, which is what a Source Unique ID is for — resubmitting the
            # same occurrence must not create a second record at the state end.
            "Source Unique ID": confirmed.get("hash") or confirmed["id"],
            "Species": sp.get("scientific", ""),
            "Date": (v.get("date") or confirmed.get("taken", ""))[:10],
            "Observer": v["by"],
            "Latitude": f"{x['lat']:.6f}",
            "Longitude": f"{x['lon']:.6f}",
            "Common Name": sp.get("common", ""),
            "Comments": notes[:900],
            "Photo URL": f"{url}/{prefix}/{confirmed['file']}" if url else "",
        })

    print(f"{len(rows)} occurrence(s) are eligible for iMapInvasives.\n")
    for r in rows[:15]:
        print(f"  {r['Species']:<32} {r['Date']}  {r['Latitude']}, {r['Longitude']}"
              f"  obs. {r['Observer']}")
    if len(rows) > 15:
        print(f"  ... and {len(rows) - 15} more")

    if blocked:
        from collections import Counter
        print(f"\n{len(blocked)} occurrence(s) not eligible:")
        for why, n in Counter(w for _, w in blocked).most_common():
            print(f"  {n:>3}  {why}")

    # iMap matches the Species column against its jurisdiction species list, so a
    # name carrying a qualifier — a bare genus, "cf.", a two-genus hedge — will not
    # match even though it is the honest thing for the catalogue to say. Better to
    # name them here than have the admin hand the whole file back.
    def unmatchable(name):
        if len(norm_sci(name).split()) < 2:
            return "genus only"
        if re.search(r"\b(cf|aff|sp|spp)\b\.?|/", name):
            return "carries a qualifier"
        return None

    rough = [(r, why) for r in rows if (why := unmatchable(r["Species"]))]
    if rough:
        print(f"\n{len(rough)} record(s) have a name iMapInvasives may not match against")
        print("its species list. Settle the identification in the field, or ask the")
        print("administrator what they want in the column:")
        for r, why in rough[:8]:
            print(f"  {r['Species']:<34} ({why})")

    if not rows:
        print("\nNothing to write. This is the expected state until someone has been")
        print("out and confirmed a regulated or invasive find — which is the whole")
        print("point: an AI identification is a lead, and a lead is not a state record.")
        print("\nRecord one with:  plantdb.py confirm --file <name> --by \"Name\" --status confirmed")
        return

    out = pathlib.Path(args.out or ROOT / "imapinvasives-export.csv")
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=IMAP_REQUIRED + IMAP_EXTRA)
        w.writeheader()
        w.writerows(rows)
    print(f"\nWrote {out}")
    print("\nBefore sending it:")
    print("  * Every Observer name must already exist in iMapInvasives — the upload")
    print("    fails otherwise. Have them make an account first, or ask the admin.")
    print("  * Ask for the current bulk-upload template. The required columns here are")
    print("    from NatureServe's published spec; the optional ones are a guess.")
    print("  * Maine's administrator is the Maine Natural Areas Program:")
    print("    chad.hammer@maine.gov / invasives.mnap@maine.gov")


def cmd_fieldwork(args):
    """A walking list: which occurrences need checking, and what would settle each.

    The survey's leads are only worth anything if someone can act on them, and
    "go and look at this" is not actionable. What a person needs standing in front
    of the plant is the feature that decides it and the thing it might be instead —
    both of which the catalogue already holds. This assembles them per occurrence,
    nearest thing to a printable page, urgent species first.
    """
    obs = [o for o in load_obs() if not o.get("local_only")]
    occs = [x for x in occurrences(obs) if not x["confirmed"]]
    if args.status:
        occs = [x for x in occs if x["origin_status"] == args.status]
    elif not args.all:
        occs = [x for x in occs if x["origin_status"] in ("regulated", "invasive")]
    if not occs:
        print("Nothing outstanding." if args.status or args.all else
              "No unconfirmed regulated or invasive occurrences. "
              "Use --all to list everything awaiting a field check.")
        return

    print(f"{len(occs)} occurrence(s) to check, most urgent first.\n")
    print("Take the photographs BEFORE you touch anything, and include something for")
    print("scale — a hand, a boot, a pen. A photo of the whole plant plus one close-up")
    print("of the feature named below is usually enough to settle an identification.\n")

    for n, x in enumerate(occs, 1):
        flag = {"regulated": "** REGULATED — Do Not Sell list **",
                "invasive": "* invasive *"}.get(x["origin_status"], x["origin_status"])
        print("=" * 72)
        print(f"{n}. {x['common']}  ({x['scientific']})   {flag}")
        print(f"   {x['lat']:.6f}, {x['lon']:.6f}"
              + (f"   spread ~{x['spread_m']:.0f} m" if x["spread_m"] > 1 else "")
              + f"   {len(x['members'])} photo(s)")
        seen = x["first_seen"] + (f" – {x['last_seen']}" if x["last_seen"] != x["first_seen"] else "")
        print(f"   photographed {seen}")
        print(f"   maps.apple.com/?ll={x['lat']:.6f},{x['lon']:.6f}&q={quote(x['common'])}")
        if x["origin_note"]:
            print(f"\n   Why it matters: {x['origin_note']}")

        note = (x["anchor"].get("note") or "").strip()
        if note:
            print(f"\n   What the photo showed: {note[:300]}")

        if x["id_marks"]:
            print("\n   CONFIRM by photographing each of these:")
            for m in x["id_marks"]:
                print(f"     [ ] {m}")
        if x["lookalikes"]:
            print("\n   RULE OUT — if any of these fits better, correct it rather than confirm:")
            for m in x["lookalikes"]:
                print(f"     - {m}")

        print(f"\n   Then:  plantdb.py confirm --file {x['anchor']['file']} \\")
        print(f"            --by \"Your Name\" --status confirmed --notes \"what you saw\"")
        print("   Photographs you take there join this occurrence automatically —")
        print(f"   anything within {OCCURRENCE_RADIUS_M} m of it is the same find.\n")


def cmd_occurrences(args):
    """Every (species, place) the survey has evidence for."""
    obs = [o for o in load_obs() if not o.get("local_only")]
    occs = occurrences(obs)
    if not args.all:
        occs = [x for x in occs if x["origin_status"] in ("regulated", "invasive", "unknown")]
    multi = sum(1 for x in occs if len(x["members"]) > 1)
    print(f"{len(occs)} occurrence(s), {multi} with more than one photograph "
          f"(grouped within {OCCURRENCE_RADIUS_M} m).\n")
    for x in occs:
        state = "confirmed" if x["confirmed"] else "unverified"
        if x["confirmed"]:
            state += f" by {x['confirmed_by']}"
            state += (f", {len(x['confirming_photos'])} photo(s) from the check"
                      if x["confirming_photos"] else ", no photo from the check")
        print(f"  {x['origin_status']:<11} {x['common'][:32]:<34} {x['lat']:.5f}, {x['lon']:.5f}"
              f"  {len(x['members'])} photo(s)  [{state}]")


def cmd_check_photos(args):
    """Do these files still carry a location and a date? Check BEFORE uploading.

    Exists because of a real and expensive surprise: 83 photographs reached the
    survey with their EXIF stripped, and it was only visible after they had been
    ingested, thumbnailed and paid for. The loss happened at export, not at upload —
    dragging out of macOS Photos hands you a rendered derivative rather than the
    file it is holding, and plain "Export…" drops GPS unless Location Information is
    ticked. Neither is visible by looking at the files, and the stripped copies
    carry a full-size but empty EXIF block, so even the file size looks plausible.

    So: export two or three, run this on the folder, upload the rest only once it
    says they are good.
    """
    folder = pathlib.Path(os.path.expanduser(args.folder)).resolve()
    if not folder.is_dir():
        sys.exit(f"Not a folder: {folder}")
    found = sorted(p for p in folder.rglob("*") if p.suffix.lower() in EXTS and p.is_file())
    if not found:
        sys.exit(f"No images found under {folder}")

    limit = args.limit or 10
    good, bad = [], []
    for p in found:
        e = exif_of(p)
        (good if (e["lat"] and e["taken"]) else bad).append((p, e))

    print(f"{len(found)} image(s) in {folder.name}\n")
    for p, e in good[:limit]:
        print(f"  ok    {p.name[:44]:<46} {e['taken'][:10]}  {e['lat']}, {e['lon']}")
    if len(good) > limit:
        print(f"  ... and {len(good) - limit} more good")
    for p, e in bad[:limit]:
        missing = " and ".join(m for m, v in (("location", e["lat"]), ("date", e["taken"])) if not v)
        print(f"  BAD   {p.name[:44]:<46} no {missing}")
    if len(bad) > limit:
        print(f"  ... and {len(bad) - limit} more bad")

    print(f"\n{len(good)} of {len(found)} carry both a location and a date.")
    if not bad:
        print("Safe to upload — every one of these can become a survey record.")
        return
    print(f"\n{len(bad)} would be ingested, identified, paid for, and then withheld:")
    print("a photograph that cannot be placed or dated is not a survey record.")
    print("\nIf these came out of macOS Photos, the metadata was lost on the way OUT of")
    print("Photos, not on the way to Drive — so moving or re-uploading them will not")
    print("bring it back. Go back to Photos and use:")
    print("\n    File > Export > Export Unmodified Original for N Photos…")
    print("\nNot drag-and-drop, which hands you a rendered derivative. Not plain")
    print("'Export…' unless 'Location Information' is ticked. Then run this again.")
    sys.exit(1)


def cmd_batches(args):
    """Batches submitted to the Batch API and not yet collected.

    A submitted batch is money already spent; the id is the only way to get the
    results. Losing it means paying again and never collecting the first run — so
    this exists to answer "is anything outstanding?" without reading the database
    by hand. Nothing else in the pipeline shows it.
    """
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import idcache
    opens = idcache.open_batches(idcache.connect())
    if not opens:
        print("No batches awaiting collection.")
        return
    print(f"{len(opens)} batch(es) submitted and not yet collected:\n")
    stale = 0
    for bid, created, n, model, region in opens:
        age = ""
        try:
            hours = (datetime.datetime.now()
                     - datetime.datetime.fromisoformat(created)).total_seconds() / 3600
            age = f", {hours:.0f}h ago"
            # The API caps a batch at 24 hours, so anything older is not still
            # running — it ended and was never collected, or it will never end.
            # Left unsaid, it sits here being polled every couple of hours forever.
            if hours > 30:
                age += "  ** older than the 24h limit — collect or investigate **"
                stale += 1
        except (TypeError, ValueError):
            pass
        print(f"  {bid}")
        print(f"      {n} photo(s), {model}, submitted {created}{age}")
    print("\nCollect them with:  .venv/bin/python scripts/identify.py --collect")
    print("A batch may take up to 24 hours; results are kept for 29 days.")
    if stale:
        print(f"\n{stale} batch(es) are past the point where they could still be running.")
        print("If --collect reports them as still processing, they are stuck: cancel them")
        print("in the console, and the photos become eligible again on the next run.")


def cmd_doctor(args):
    """Report what is configured and what still blocks a real survey run."""
    ok, todo = [], []

    def check(cond, good, bad):
        (ok if cond else todo).append(good if cond else bad)

    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

    venv = ROOT / ".venv" / "bin" / "python"
    check(venv.exists(), "Python venv present",
          "No .venv — run: python3 -m venv .venv && .venv/bin/pip install anthropic")
    if venv.exists():
        has = subprocess.run([str(venv), "-c", "import anthropic"], capture_output=True).returncode == 0
        check(has, "anthropic SDK installed", "anthropic SDK missing — .venv/bin/pip install anthropic")

    check(env.exists(), ".env present (gitignored)", "No .env — identification and R2 read their secrets from it")
    check(bool(os.environ.get("ANTHROPIC_API_KEY")), "ANTHROPIC_API_KEY set",
          "ANTHROPIC_API_KEY not set — identify.py cannot run")

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import r2
    cfg = load(PUBCFG_F, {})
    check(bool(cfg.get("r2_public_base")), "R2 public base URL configured",
          "r2_public_base empty in data/publish-config.json — images would be bundled into git")
    if r2.config():
        good, msg = r2.check(r2.config())
        check(good, "R2 S3 credentials work", f"R2 credentials rejected: {msg}")
    else:
        todo.append("R2 S3 credentials not set — uploads fall back to wrangler, whose OAuth "
                    "expires and cannot refresh unattended. Required before the watcher runs.")

    area = cfg.get("survey_area")
    check(bool(area), f"Survey area set ({area.get('name')})" if area else "",
          "No survey_area — nothing stops out-of-area photos being published")
    if area:
        pub = public_obs()
        ac = area_check(pub)
        if ac and ac[2] and not ac[3]:
            todo.append(f"{len(ac[2])} published record(s) are stand-in data outside "
                        f"{area.get('name')} — scrub before or as the first island batch lands")

    check(bool(cfg.get("notice")) is False, "No proof-of-concept notice (real data)",
          "Proof-of-concept notice still shown — remove `notice` from publish-config.json when real data lands")

    # Contributor mode. Optional throughout — without it the site is the read-only
    # survey it has always been — so every branch here is informational, and none
    # of it can make `doctor` fail.
    try:
        import inbox as _ib
        endpoint = cfg.get("contributor_endpoint")
        drain = _ib.config()
        if not endpoint and not drain:
            print("  --   Contributor mode off (no contributor_endpoint in "
                  "publish-config.json) — the site is read-only")
        else:
            check(bool(endpoint), "Contributor endpoint published to the site",
                  "SIF_WORKER_URL is set but publish-config.json has no "
                  "contributor_endpoint — nobody can sign in on the site")
            check(bool(drain), "Contributor inbox drain configured",
                  "The site can accept submissions but the pipeline cannot collect them ("
                  + ", ".join(_ib.missing_vars()) + ") — they would queue up unapplied")
            if drain:
                ok_i, msg = _ib.check(drain)
                check(ok_i, f"Contributor inbox reachable — {msg}",
                      f"Contributor inbox unreachable: {msg}")
            if endpoint and not str(endpoint).startswith("https://"):
                todo.append(f"contributor_endpoint is {endpoint} — a contributor token "
                            "would travel unencrypted; use https:// outside local testing")
    except ImportError:
        pass

    tn = have_thumbnailer()
    check(bool(tn), f"Thumbnailer available ({tn})",
          "No way to make a thumbnail — install Pillow (`pip install Pillow pillow-heif`) "
          "or run on macOS, which has `sips`. Without one, ingest skips every photo.")

    # Scheduling is satisfied by either the cloud workflow or the local launchd
    # agent — reporting the laptop watcher as outstanding when the pipeline runs
    # hourly in Actions would be telling you to fix something that is not broken.
    plist = pathlib.Path.home() / "Library/LaunchAgents/com.mueller.searsisland.plist"
    cloud = (ROOT / ".github/workflows/survey-pipeline.yml").exists()
    check(plist.exists() or cloud,
          "Pipeline scheduled in GitHub Actions (nightly batch, collected every 2h)"
          if cloud else "Watcher installed",
          "Nothing runs the pipeline on a schedule — either add the Actions workflow "
          "(see the README) or ./scripts/install-watcher.sh <folder>. Manual runs work.")
    if cloud and plist.exists():
        todo.append("Both the cloud workflow and the local watcher are active — they will "
                    "race to commit data/. Stop one:  launchctl unload "
                    "~/Library/LaunchAgents/com.mueller.searsisland.plist")

    print("READY:")
    for x in ok:
        print(f"  ok   {x}")
    print("\nSTILL NEEDED:" if todo else "\nNothing outstanding.")
    for x in todo:
        print(f"  --   {x}")
    return 1 if todo else 0


def cmd_remove(args):
    """Delete records entirely — from the data, from git, and from R2.

    For retiring stand-in data once real survey photos replace it. Deleting the
    records alone would strand their images on R2 forever, so this removes the
    objects too. Previews by default; needs --yes.
    """
    obs = load_obs()
    sel = [o for o in obs if (not args.batch or o.get("batch") == args.batch)
           and (not args.file or o["file"] in set(args.file))]
    if not (args.batch or args.file):
        sys.exit("Refusing to remove everything — pass --batch or --file.")
    if not sel:
        print("Nothing matches.")
        return

    pub = sum(1 for o in sel if not o.get("local_only"))
    print(f"{len(sel)} record(s) would be deleted ({pub} of them published).")
    for o in sel[:8]:
        print(f"  {o['file'][:12]}…  {o.get('species_id','?'):26} {o.get('batch','')}")
    if len(sel) > 8:
        print(f"  ... and {len(sel) - 8} more")
    print("\nThis removes the records, their thumbnails, and their R2 objects.")
    print("Originals are NOT touched — an uploaded photograph stays in the contributor\n"
          "inbox bucket and a Drive one stays in Drive, so this is recoverable.")
    if not args.yes:
        print("\nNothing changed. Re-run with --yes to delete.")
        return

    cfg_pub = load(PUBCFG_F, {})
    prefix = (cfg_pub.get("r2_prefix") or "thumbs").strip("/")
    bucket = cfg_pub.get("r2_bucket", "")
    manifest = load(R2_MANIFEST, {})
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import r2
    creds = r2.config()

    # Only objects actually on R2 need deleting. Most withheld records were never
    # uploaded — publish only sends what it publishes — so without this the run
    # spends a network round trip per record to delete something that isn't there.
    hosted = [o for o in sel if not o.get("local_only") and o["file"] in manifest]

    if hosted and not creds:
        # The wrangler fallback spawns a Node process per object with a 120s
        # timeout, and its OAuth expires and cannot refresh unattended. At any real
        # batch size it looks exactly like a hang, which is how this command came to
        # be interrupted halfway with nothing saved. Say so instead of starting.
        print(f"\n{len(hosted)} of these have an image on R2, and R2 credentials are "
              f"not set ({', '.join(r2.missing_vars())}).")
        print("Deleting them would fall back to `npx wrangler`, one process per object,")
        print("which at this size will look like a hang. Load the credentials first:")
        print("\n    set -a && . ./.env && set +a")
        print(f"\nOr pass --keep-images to remove the {len(sel)} record(s) and leave the")
        print("images on R2 (they become unreferenced; `publish --prune-r2` clears those).")
        sys.exit(1)

    gone = 0
    for o in sel:
        if o in hosted and not args.keep_images:
            key = f"{prefix}/{o['file']}"
            try:
                r2.delete(creds, key)
                gone += 1
            except Exception as e:
                print(f"  ! could not delete {key}: {e}")
        manifest.pop(o["file"], None)
        thumb_path(o).unlink(missing_ok=True)

    drop = {o["file"] for o in sel}
    obs = [o for o in obs if o["file"] not in drop]
    save_obs(obs)

    # Removing records orphans the catalogue entries they created — an auto entry
    # exists only because a photo matched it. Left behind, they are species the
    # survey claims to have found with nothing standing behind them, which is how
    # retiring the Orono batch left a regulated knotweed entry on a survey of an
    # island it was never photographed on. Reconcile here so `remove` cannot leave
    # that state, rather than relying on someone remembering the second command.
    species = load(SPECIES_F, [])
    dropped, merged, _ = reconcile(species, obs, apply=True)
    if dropped or merged:
        save(SPECIES_F, species)
        save_obs(obs)
        for sp, reason, _ in dropped:
            print(f"  dropped catalogue entry {sp['id']} — {reason}")
        for keep, _, losers, _ in merged:
            print(f"  merged {', '.join(l['id'] for l in losers)} into {keep['id']}")
    save(R2_MANIFEST, manifest)
    # The location sidecar is written at ingest and never read, so an entry left
    # behind here is invisible — but the file is tracked, so orphans accumulate in
    # git for the life of the project. Removing a record removes everything the
    # record put anywhere.
    private = load(PRIVATE_F, {})
    if any(f in private for f in drop):
        save(PRIVATE_F, {k: v for k, v in private.items() if k not in drop})
    cmd_build(args)
    print(f"\nRemoved {len(sel)} record(s); {gone} object(s) deleted from R2.")
    print("Run `publish` and push to update the site.")


def cmd_promote(args):
    """Move local-only records into the published set.

    Deliberately explicit and one-directional in intent: publishing a record makes
    its precise coordinates public, and a git push cannot be taken back. Prints
    exactly what will become public and requires --yes to act.
    """
    obs = load_obs()
    sel = [o for o in obs if o.get("local_only")
           and (not args.batch or o.get("batch") == args.batch)
           and (not args.file or o["file"] in set(args.file))]
    if not sel:
        print("Nothing matches — no local-only records with that batch/file.")
        return

    lats = [float(o["lat"]) for o in sel if o.get("lat")]
    lons = [float(o["lon"]) for o in sel if o.get("lon")]
    print(f"{len(sel)} record(s) would become public, with precise coordinates:\n")
    for o in sel[:12]:
        print(f"  {o['file'][:12]}…  {o.get('species_id','?'):28} {o.get('lat','')}, {o.get('lon','')}")
    if len(sel) > 12:
        print(f"  ... and {len(sel) - 12} more")
    if lats:
        print(f"\nThey span {(max(lats)-min(lats))*111000:.0f} m N-S by "
              f"{(max(lons)-min(lons))*79000:.0f} m E-W, centred on "
              f"{sum(lats)/len(lats):.5f}, {sum(lons)/len(lons):.5f}")
        print("Check that against where you live before publishing — a tight cluster of")
        print("dated, precise points describes a routine, not just a plant list.")

    if not args.yes:
        print("\nNothing changed. Re-run with --yes to publish these.")
        return

    files = {o["file"] for o in sel}
    moved = 0
    for o in obs:
        if o.get("local_only") and o["file"] in files:
            src, dst = THUMBS_LOCAL / o["file"], THUMBS / o["file"]
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
            o.pop("local_only", None)
            moved += 1
    save_obs(obs)
    cmd_build(args)
    print(f"\nPromoted {moved} record(s). They are now in {OBS_F.name} and will publish.")
    print("To undo before pushing:  git checkout -- data/observations.json")


# --- Catalogue hygiene -----------------------------------------------------
# An automatically written catalogue fails in two ways that cannot be prevented at
# the moment of writing, only repaired afterwards:
#
#   1. NEAR-DUPLICATES. Every batch request is built from the catalogue as it stood
#      when the batch was submitted, so no request can see an entry created by a
#      sibling request in the same batch. Two photos of the same lichen therefore
#      mint two entries ("Pixie-cup Lichen" and "Pixie Cup Lichen (trumpet
#      lichen)"). Nothing inside a request can fix this — it is reconciled after
#      collection.
#
#   2. DESCRIPTIONS POSING AS SPECIES. "Fern (unidentified colony)" describes a
#      photograph, not an organism. identify.py refuses to create these now, but
#      the catalogue is sent with every future photo, so one that got in keeps
#      offering itself as a match. It has to be taken back out.
#
# The tests below are shared with identify.py so the gate that refuses to create
# these and the pass that removes them can never disagree about what one is.

# Ranks above genus have standardised suffixes in botanical and mycological
# nomenclature. A "scientific name" ending in one of them names a group, not an
# organism: "Bryophyta sp." is the mosses — all of them. It is the reliable tell
# that an answer is a description rather than an identification. A genus is the
# coarsest rank this survey treats as a finding, which is also what the identifier
# is told to under-claim to.
ABOVE_GENUS = ("phyta", "phytina", "phyceae", "opsida", "mycota", "mycotina",
               "mycetes", "ales", "aceae", "oideae")

# Words that mark a name as a non-answer. Deliberately does NOT include "possible",
# "probable" or "cf." — "Japanese Knotweed (possible young shoot)" is a hedged
# claim about a real species, and hedged invasive leads are the point of the
# survey, not noise to be filtered out.
HEDGE_WORDS = ("unidentified", "unidentifiable", "indeterminate", "indet",
               "unknown", "unnamed", "mixed", "assorted", "various")


def norm_common(name):
    """Common name reduced to comparable form: no case, punctuation or parentheticals.

    'Pixie-cup Lichen' and 'Pixie Cup Lichen (trumpet lichen)' both land on
    'pixie cup lichen', which is what makes them detectably the same entry.
    """
    s = re.sub(r"\([^)]*\)", " ", (name or "").lower())
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


def norm_sci(name):
    """Scientific name reduced to genus (+ epithet), dropping qualifiers.

    'Cladonia sp. (pyxidata/chlorophaea group)' and 'Cladonia sp.' both reduce to
    'cladonia'. Two tokens means a binomial; one means genus only.
    """
    s = re.sub(r"\([^)]*\)", " ", (name or "").lower())
    s = re.sub(r"\b(sp|spp|cf|aff|var|subsp|ssp|sect|group|complex|agg)\b\.?", " ", s)
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split()[:2])


def non_answer_reason(sp):
    """Why this catalogue entry is a description rather than a species, or None.

    Two independent tests, either sufficient: a hedge word in the common name, and
    a scientific name that is not a genus. They agree on every case seen so far,
    which is the point — one of them catches a naming style the other misses.
    """
    # Hedge words are looked for in the WHOLE name, parentheticals included —
    # unlike norm_common, which drops them so that "Pixie-cup Lichen" and "Pixie
    # Cup Lichen (trumpet lichen)" compare equal for merging. The qualifier is
    # exactly where the hedging lives: "Fern (unidentified colony)" with a real
    # genus in `scientific` would otherwise pass both tests and be written to the
    # catalogue under a name that invites the next photo to match it.
    whole = " ".join(re.sub(r"[^a-z0-9]+", " ", (sp.get("common") or "").lower()).split())
    for w in HEDGE_WORDS:
        if re.search(rf"\b{w}", whole):
            return f'"{w}" in the name — a description of the photo, not a taxon'
    sci = norm_sci(sp.get("scientific"))
    if not sci:
        return "no scientific name at all"
    head = sci.split()[0]
    for suf in ABOVE_GENUS:
        if head.endswith(suf):
            return f"'{sp.get('scientific')}' is a rank above genus, not an organism"
    return None


def _refcount(obs, sid):
    """How many records lean on this species id, by any route."""
    n = 0
    for o in obs:
        if o.get("species_id") == sid or sid in (o.get("also") or []):
            n += 1
        elif (o.get("verified") or {}).get("species_id") == sid:
            n += 1
    return n


def dup_groups(species):
    """Group entries that are the same organism written up twice.

    Merged on exact agreement after normalisation — same common name, or the same
    binomial. A shared genus alone is NOT enough: two Cladonia species are two
    species, and collapsing them would destroy a real distinction rather than a
    duplicated one. Those are reported as candidates instead.
    """
    parent = {sp["id"]: sp["id"] for sp in species}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    buckets = {}
    for sp in species:
        if sp["id"] == "unknown":
            continue
        if (c := norm_common(sp.get("common"))):
            buckets.setdefault(("common", c), []).append(sp["id"])
        s = norm_sci(sp.get("scientific"))
        if len(s.split()) == 2:                      # a binomial names one species
            buckets.setdefault(("sci", s), []).append(sp["id"])
    for ids in buckets.values():
        for other in ids[1:]:
            ra, rb = find(ids[0]), find(other)
            if ra != rb:
                parent[ra] = rb

    groups = {}
    for sp in species:                               # file order == creation order
        if sp["id"] != "unknown":
            groups.setdefault(find(sp["id"]), []).append(sp)
    return [g for g in groups.values() if len(g) > 1]


def near_miss_groups(species, merged_ids):
    """Genus-level entries sitting next to named ones in the same genus.

    Only reported when at least one side is genus-only, because that is the case
    where the two might be the same plant written up twice. Two full binomials in
    one genus are simply two species — Trifolium pratense and Trifolium repens are
    not a near-duplicate, and reporting them every run would train the eye to skip
    this section.
    """
    by_genus = {}
    for sp in species:
        if sp["id"] == "unknown" or sp["id"] in merged_ids:
            continue
        if (g := norm_sci(sp.get("scientific")).split()):
            by_genus.setdefault(g[0], []).append(sp)
    return [v for v in by_genus.values() if len(v) > 1
            and any(len(norm_sci(sp.get("scientific")).split()) == 1 for sp in v)]


def _repoint(obs, mapping):
    """Rewrite every species reference a record holds.

    `verified.species_id` is included on purpose. A merge renames a species; it
    does not overrule anybody's verdict. The human's answer still says exactly what
    it said, under the id that survived — which is the only way it stays true.
    Nothing else under `verified` is touched.
    """
    for o in obs:
        if o.get("species_id") in mapping:
            o["species_id"] = mapping[o["species_id"]]
        v = o.get("verified") or {}
        if v.get("species_id") in mapping:
            v["species_id"] = mapping[v["species_id"]]
        if (also := o.get("also")):
            seen, new = set(), []
            for a in also:
                a = mapping.get(a, a)
                if a != o.get("species_id") and a not in seen:
                    seen.add(a)
                    new.append(a)
            if new:
                o["also"] = new
            else:
                o.pop("also")


def reconcile(species, obs, apply, log=print):
    """Drop descriptions, merge duplicates. Returns (dropped, merged, renames).

    Order matters: the drop pass runs first so a merge never has to choose between
    two entries that both should not exist.
    """
    dropped, merged, renames = [], [], {}
    auto = [sp for sp in species if str(sp.get("source", "")).startswith("auto (")]

    # Only auto-created entries are ever dropped. The seed catalogue contains
    # hedged entries a person put there deliberately ("Bolete (unidentified)"),
    # and an unattended run must not quietly delete somebody's editorial choice.
    # An entry a human has verified a record against is likewise off limits: that
    # would be deleting the target of a field check, which is not ours to do.
    verified_ids = {(o.get("verified") or {}).get("species_id") for o in obs}
    for sp in auto:
        reason = non_answer_reason(sp)
        if not reason:
            continue
        if sp["id"] in verified_ids:
            log(f"  keeping {sp['id']} — {reason}, but a person has verified a record "
                f"against it. Fix it by hand or with `confirm`.")
            continue
        dropped.append((sp, reason, _refcount(obs, sp["id"])))

    if apply and dropped:
        gone = {sp["id"]: (sp, reason) for sp, reason, _ in dropped}
        for o in obs:
            sid = o.get("species_id")
            if sid in gone:
                sp, reason = gone[sid]
                o["species_id"] = "unknown"
                # Not "unidentified": that would put the photo back in the queue for
                # the next ordinary run, which would buy the same non-answer again.
                # It stays visible to `todo` and to --all-unknown, where a person has
                # chosen to spend money on it.
                o["confidence"] = "low"
                note = (o.get("note") or "").strip()
                o["note"] = (note + " " if note else "") + (
                    f'[Catalogue entry "{sp["common"]}" removed: {reason}. '
                    f"This photo remains unidentified.]")
            if (also := o.get("also")):
                keep = [a for a in also if a not in gone]
                if keep:
                    o["also"] = keep
                else:
                    o.pop("also")
        species[:] = [sp for sp in species if sp["id"] not in gone]

    for group in dup_groups(species):
        keep = group[0]                                  # earliest: its id is published
        best = max(group, key=lambda sp: len(json.dumps(sp, ensure_ascii=False)))
        losers = [sp for sp in group if sp["id"] != keep["id"]]
        merged.append((keep, best, losers, [_refcount(obs, sp["id"]) for sp in group]))
        renames.update({sp["id"]: keep["id"] for sp in losers})

    if apply and merged:
        for keep, best, losers, _ in merged:
            i = next(n for n, sp in enumerate(species) if sp["id"] == keep["id"])
            # Keep the oldest id — it may already be a link on the published site —
            # but the fullest write-up, which is what a reader is actually served by.
            entry = {**best, "id": keep["id"]}
            entry["merged_from"] = sorted({sp["id"] for sp in losers}
                                          | set(entry.get("merged_from") or []))
            species[i] = entry
        drop_ids = set(renames)
        species[:] = [sp for sp in species if sp["id"] not in drop_ids]
        _repoint(obs, renames)

    # Orphans. An auto-created entry exists only because a photo matched it, so when
    # those records go — `remove --batch`, a re-identification, a correction — the
    # entry is left standing with nothing behind it and the site publishes a species
    # nobody photographed here. That is how retiring the Orono proof-of-concept
    # batch left "Japanese Knotweed (regulated)" on a survey of an island it was
    # never photographed on: exactly the false positive this project cannot afford.
    #
    # Last, so a duplicate is merged into its survivor rather than orphaned, and a
    # description is dropped for the honest reason rather than this one.
    already = {sp["id"] for sp, _, _ in dropped} | set(renames)
    orphans = [
        (sp, "nothing references it — the records it was created from are gone", 0)
        for sp in species
        if str(sp.get("source", "")).startswith("auto (")
        and sp["id"] not in already
        and sp["id"] not in verified_ids
        and not _refcount(obs, sp["id"])
    ]
    if apply and orphans:
        gone = {sp["id"] for sp, _, _ in orphans}
        species[:] = [sp for sp in species if sp["id"] not in gone]
    dropped += orphans

    return dropped, merged, renames


def cmd_reconcile(args):
    """Repair the catalogue after a batch: drop descriptions, merge duplicates.

    Runs automatically after `identify.py --collect`; also available on its own,
    because the entries already in the catalogue predate the gate that now stops
    them being written.
    """
    species, obs = load(SPECIES_F, []), load_obs()
    dropped, merged, renames = reconcile(species, obs, apply=False)

    if dropped:
        print(f"{len(dropped)} auto-created entr(ies) do not belong in the catalogue:\n")
        for sp, reason, n in dropped:
            flag = {"regulated": "  ** REGULATED **", "invasive": "  * invasive *"}.get(
                sp.get("origin_status"), "")
            print(f"  {sp['id']}{flag}")
            print(f"      {sp['common']!r}  ({sp['scientific']})")
            print(f"      {reason}")
            if n:
                print(f"      {n} record(s) would go back to unidentified")
        print()
    if merged:
        print(f"{len(merged)} near-duplicate group(s):\n")
        for keep, best, losers, counts in merged:
            print(f"  {keep['id']}  ←  {', '.join(sp['id'] for sp in losers)}")
            print(f"      {keep['common']!r}  ({keep['scientific']})")
            src = "its own" if best["id"] == keep["id"] else f"{best['id']}'s (fuller)"
            print(f"      keeping the id {keep['id']!r} and {src} write-up")
            print(f"      {sum(counts)} record(s) would end up on it")
        print()

    near = near_miss_groups(species, set(renames) | {sp["id"] for sp, _, _ in dropped})
    if near:
        print("Same genus, different names — NOT merged automatically; two species in")
        print("one genus are two species. Check these yourself:\n")
        for g in near:
            for sp in g:
                print(f"  {sp['id']:<40} {sp['common']}  ({sp['scientific']})")
            print()

    if not dropped and not merged:
        print("Catalogue is clean — nothing to drop, nothing to merge.")
        return
    if not args.yes:
        print("Nothing changed. Re-run with --yes to apply.")
        return

    reconcile(species, obs, apply=True)
    save(SPECIES_F, species)
    save_obs(obs)
    if renames:
        # The identification cache is keyed by photo, and remembers which species
        # each result resolved to. Left stale, replaying a cached result would find
        # its id missing and mint the duplicate all over again.
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
        import idcache
        con = idcache.connect()
        for old, new in renames.items():
            con.execute("UPDATE identifications SET species_id = ? WHERE species_id = ?",
                        (new, old))
        con.commit()
        print(f"Repointed {len(renames)} id(s) in the identification cache too.")
    cmd_build(args)
    print(f"\nDropped {len(dropped)}, merged {len(merged)} group(s). "
          f"{len(species)} species remain.")
    print("Run `publish` and push to update the site.")


def cmd_serve(args):
    """Preview the local app, with caching disabled.

    Browsers hold on to index.html aggressively, so an edit to the app can look
    like it did nothing — or worse, like the data is broken — until a hard reload.
    Serving no-store removes that whole class of confusion while iterating.
    """
    import http.server, functools
    cmd_build(args)

    class H(http.server.SimpleHTTPRequestHandler):
        def end_headers(self):
            self.send_header("Cache-Control", "no-store, must-revalidate")
            super().end_headers()

        def log_message(self, *a):
            pass

    handler = functools.partial(H, directory=str(ROOT))
    try:
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    except OSError as e:
        if e.errno != errno.EADDRINUSE:
            raise
        # Almost always an earlier `serve` still running — this one is easy to
        # start and easy to forget, and the bare traceback names neither the
        # command that took the port nor a way out of it.
        who = subprocess.run(["lsof", "-nP", f"-iTCP:{args.port}", "-sTCP:LISTEN"],
                             capture_output=True, text=True).stdout.strip().splitlines()
        print(f"Port {args.port} is already in use.")
        for line in who[1:3]:
            parts = line.split()
            print(f"  held by pid {parts[1]} ({parts[0]})")
        if len(who) > 1:
            print(f"\nStop it:      kill {who[1].split()[1]}")
        print(f"Or pick another port:   plantdb.py serve --port {args.port + 1}")
        sys.exit(1)
    print(f"Serving {ROOT.name} at http://localhost:{args.port}/   (ctrl-c to stop)")
    print("Caching is disabled — just reload after a rebuild.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


def cmd_species(args):
    for s in sorted(load(SPECIES_F, []), key=lambda s: s["common"]):
        print(f"  {s['id']:<28} {s['common']}  ({s['edibility']})")


def cmd_todo(args):
    obs = load_obs()
    pend = [o for o in obs if o["species_id"] == "unknown"]
    if not pend:
        print("Everything is identified.")
        return
    print(f"{len(pend)} photo(s) awaiting identification:\n")
    for o in pend:
        print(f"  {o['file']}   [{o.get('batch','')}]  {o.get('note','')}")
    print("\nTo tag one, edit data/observations.json — set \"species_id\" to an id from")
    print("`plantdb.py species` (add a new entry to data/species.json if it's a new plant),")
    print("then run `python3 scripts/plantdb.py build`.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("ingest", help="add every photo in a folder (recursively)")
    i.add_argument("folder")
    i.add_argument("--batch", help="label for this group of photos (defaults to the folder name)")
    i.add_argument("--local", action="store_true",
                   help="keep these records out of git and off the published site — "
                        "use for test photos or anywhere outside the survey area")
    i.set_defaults(func=cmd_ingest)
    sub.add_parser("build", help="regenerate app/data.js").set_defaults(func=cmd_build)
    cf = sub.add_parser("confirm", help="record that a person verified a record in the field")
    cf.add_argument("--file", nargs="+", required=True, help="filename(s) or record id(s)")
    cf.add_argument("--by", required=True, help="who checked it — a name, not an initial")
    cf.add_argument("--status", required=True, choices=VERIFY_STATUS)
    cf.add_argument("--species", help="the correct species id (required with --status corrected)")
    cf.add_argument("--date", help="date of the check (defaults to today)")
    cf.add_argument("--notes", help="what they saw")
    cf.set_defaults(func=cmd_confirm)
    uv = sub.add_parser("unverified", help="what still needs a person to go and look")
    uv.add_argument("--status", help="only this regulatory status (e.g. regulated)")
    uv.add_argument("--limit", type=int)
    uv.set_defaults(func=cmd_unverified)
    sub.add_parser("drive-folders", help="list Drive folders the service account can see")\
       .set_defaults(func=cmd_drive_folders)
    dr = sub.add_parser("ingest-drive", help="fetch new photos from the shared Drive folder")
    dr.add_argument("--batch", help="label for this group (defaults to today)")
    dr.add_argument("--local", action="store_true",
                    help="keep these records out of git and off the published site")
    dr.add_argument("--limit", type=int, help="only fetch this many (good for a first run)")
    dr.set_defaults(func=cmd_ingest_drive)
    ip = sub.add_parser("inbox-pull", help="apply what contributors submitted through the site")
    ip.add_argument("--yes", action="store_true", help="apply (otherwise just previews)")
    ip.add_argument("--force", action="store_true",
                    help=f"allow withdrawing more than {MAX_UNATTENDED_CLEARS} verifications at once")
    ip.set_defaults(func=cmd_inbox_pull)
    ct = sub.add_parser("contributor", help="mint, list and revoke contributor sign-in tokens")
    ct.add_argument("action", choices=["list", "add", "revoke", "bootstrap"])
    ct.add_argument("--name", help="the person's name — this becomes 'verified by'")
    ct.add_argument("--role", default="contributor",
                    help=f"one of {', '.join(ROLES)} (default: contributor)")
    ct.add_argument("--id", help="which token to revoke (see `contributor list`)")
    ct.set_defaults(func=cmd_contributor)
    rd = sub.add_parser("redundant", help="mark a photograph surplus to another of the same find")
    rd.add_argument("--file", required=True, help="the surplus photograph")
    rd.add_argument("--of", help="the photograph that represents the find")
    rd.add_argument("--by", help="who decided — a name, not an initial")
    rd.add_argument("--species", help="the species it is surplus for (default: what both are)")
    rd.add_argument("--date", help="when (defaults to today)")
    rd.add_argument("--notes", help="why")
    rd.add_argument("--unmark", action="store_true", help="carry this photograph again")
    rd.set_defaults(func=cmd_redundant)
    wd = sub.add_parser("withdraw", help="take a record off the site (reversible), or restore it")
    wd.add_argument("--file", required=True, help="filename or record id")
    wd.add_argument("--by", help="who decided — a name, not an initial")
    wd.add_argument("--reason", help="why — this is the only record of it")
    wd.add_argument("--date", help="when (defaults to today)")
    wd.add_argument("--restore", action="store_true", help="put it back on the site")
    wd.set_defaults(func=cmd_withdraw)
    wh = sub.add_parser("withheld", help="everything the site is not carrying, and why")
    wh.add_argument("--limit", type=int, help="how many to list per reason")
    wh.set_defaults(func=cmd_withheld)
    sub.add_parser("cache", help="what we've already paid to identify, and what it cost").set_defaults(func=cmd_cache)
    fw = sub.add_parser("fieldwork", help="what to go and check, and what would settle each")
    fw.add_argument("--status", help="only this regulatory status")
    fw.add_argument("--all", action="store_true", help="include natives and undetermined")
    fw.set_defaults(func=cmd_fieldwork)
    oc = sub.add_parser("occurrences", help="species-and-place groupings, not one row per photo")
    oc.add_argument("--all", action="store_true", help="include natives and introduced")
    oc.set_defaults(func=cmd_occurrences)
    cp = sub.add_parser("check-photos",
                        help="do these files still carry a location and date? run BEFORE uploading")
    cp.add_argument("folder")
    cp.add_argument("--limit", type=int, help="how many of each to list (default 10)")
    cp.set_defaults(func=cmd_check_photos)
    ei = sub.add_parser("export-imap",
                        help="field-verified invasive records as an iMapInvasives bulk-upload CSV")
    ei.add_argument("--out", help="where to write the CSV (default: imapinvasives-export.csv)")
    ei.set_defaults(func=cmd_export_imap)
    sub.add_parser("batches", help="batches submitted to the Batch API and not yet collected")\
       .set_defaults(func=cmd_batches)
    sub.add_parser("doctor", help="report what is configured and what still blocks a run").set_defaults(func=cmd_doctor)
    rm = sub.add_parser("remove", help="delete records, their thumbnails and their R2 objects")
    rm.add_argument("--batch", help="every record from this batch")
    rm.add_argument("--file", nargs="*", help="these specific filenames")
    rm.add_argument("--yes", action="store_true", help="actually delete (otherwise just previews)")
    rm.add_argument("--keep-images", action="store_true",
                    help="remove the records but leave their images on R2")
    rm.set_defaults(func=cmd_remove)
    pr = sub.add_parser("promote", help="move local-only records into the published set")
    pr.add_argument("--batch", help="only records from this batch")
    pr.add_argument("--file", nargs="*", help="only these specific filenames")
    pr.add_argument("--yes", action="store_true", help="actually do it (otherwise just previews)")
    pr.set_defaults(func=cmd_promote)
    sv = sub.add_parser("serve", help="preview the local app in a browser (no caching)")
    sv.add_argument("--port", type=int, default=8777)
    sv.set_defaults(func=cmd_serve)
    rg = sub.add_parser("refresh-gps", help="re-read coordinates from the originals in photos/")
    rg.add_argument("--force", action="store_true", help="overwrite coordinates that are already set")
    rg.set_defaults(func=cmd_refresh_gps)
    rc = sub.add_parser("reconcile", help="merge duplicate species and drop descriptions posing as species")
    rc.add_argument("--yes", action="store_true", help="actually apply it")
    rc.set_defaults(func=cmd_reconcile)
    sub.add_parser("species", help="list species ids").set_defaults(func=cmd_species)
    sub.add_parser("todo", help="list photos not yet identified").set_defaults(func=cmd_todo)
    pb = sub.add_parser("publish", help="build public/, uploading images to R2 if configured")
    pb.add_argument("--no-upload", action="store_true",
                    help="skip the R2 upload (the deploy runner uses this — it has no credentials)")
    pb.add_argument("--prune-r2", action="store_true",
                    help="also delete R2 objects the site no longer references")
    pb.set_defaults(func=cmd_publish)
    sub.add_parser("scrub", help="strip EXIF from thumbnails and blur tracked coordinates").set_defaults(func=cmd_scrub)
    sub.add_parser("verify", help="check nothing tracked carries precise location data").set_defaults(func=cmd_verify)
    inv = sub.add_parser("invasives", help="survey report: non-native species with dates and locations")
    inv.add_argument("--all", action="store_true", help="include native species too")
    inv.set_defaults(func=cmd_invasives)
    a = ap.parse_args()
    a.func(a)
