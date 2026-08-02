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
import argparse, hashlib, json, os, pathlib, shutil, subprocess, sys, datetime

ROOT = pathlib.Path(__file__).resolve().parent.parent
PHOTOS, THUMBS, DATA = ROOT / "photos", ROOT / "thumbs", ROOT / "data"
# thumbs/ is tracked — it's the only way the deploy runner gets images. So a
# local-only photo's thumbnail needs somewhere gitignored to live, or `git add -A`
# in autopilot.sh would commit it regardless of where its record is stored.
THUMBS_LOCAL = ROOT / "thumbs-local"
SPECIES_F, OBS_F = DATA / "species.json", DATA / "observations.json"
# Local-only records: gitignored, so they can never be committed or published.
# Which file a record lives in IS the marker — there is no flag to forget to set.
# Use this for anything shot outside the survey area, e.g. test photos from a
# populated town, where the "precise location is the deliverable" argument does
# not hold and the upstream reasons for blurring do.
LOCAL_OBS_F = DATA / "observations-local.json"
PRIVATE_F = DATA / "locations-private.json"   # gitignored: full-precision coords
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
    return json.load(open(p)) if p.exists() else default


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


def thumb_dir(o):
    return THUMBS_LOCAL if o.get("local_only") else THUMBS


def thumb_path(o):
    return thumb_dir(o) / o["file"]


def public_obs():
    """Only what may be published: public file, minus anything the screener rejected."""
    return [o for o in load(OBS_F, []) if not o.get("rejected")]


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


def exif_of(path):
    try:
        out = subprocess.run(
            ["mdls", "-name", "kMDItemContentCreationDate", "-name", "kMDItemLatitude",
             "-name", "kMDItemLongitude", "-raw", str(path)],
            capture_output=True, text=True, timeout=20).stdout.split("\x00")
    except Exception:
        out = []
    out = [x.strip() for x in (out + ["", "", ""])[:3]]
    out = ["" if x in ("(null)", "null") else x for x in out]
    if not out[0]:
        ts = datetime.datetime.fromtimestamp(path.stat().st_mtime)
        out[0] = ts.strftime("%Y-%m-%d %H:%M:%S +0000")
    return {"taken": out[0], "lat": out[1], "lon": out[2]}


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


def make_thumb(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["sips", "-Z", str(THUMB_PX), "-s", "format", "jpeg", str(src),
                        "--out", str(dst)], capture_output=True)
    if r.returncode != 0 or not dst.exists():
        return False
    strip_exif(dst)
    return True


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
        h = sha(src)
        if h in known:
            skipped += 1
            continue
        stem = src.stem
        dest = PHOTOS / (stem + ".jpg")
        n = 1
        while dest.exists():
            dest = PHOTOS / f"{stem}-{n}.jpg"
            n += 1
        PHOTOS.mkdir(exist_ok=True)
        if src.suffix.lower() in (".jpg", ".jpeg"):
            shutil.copy2(src, dest)
        else:
            if subprocess.run(["sips", "-s", "format", "jpeg", str(src), "--out", str(dest)],
                              capture_output=True).returncode != 0:
                print(f"  ! could not convert {src.name}")
                continue
        if not make_thumb(dest, (THUMBS_LOCAL if args.local else THUMBS) / dest.name):
            print(f"  ! could not thumbnail {dest.name}")
        e = exif_of(dest)
        # Precise coordinates go to the gitignored sidecar; the tracked record is blurred.
        private = load(PRIVATE_F, {})
        private[dest.name] = {"lat": e["lat"], "lon": e["lon"], "taken": e["taken"]}
        save(PRIVATE_F, private)
        rec = {"id": dest.stem, "file": dest.name, "species_id": "unknown",
               "confidence": "unidentified", "note": "", "taken": e["taken"],
               "lat": blur(e["lat"]), "lon": blur(e["lon"]), "batch": batch, "hash": h}
        if args.local:
            rec["local_only"] = True
        obs.append(rec)
        known.add(h)
        added += 1
        print(f"  + {dest.name}")

    obs.sort(key=lambda o: o.get("taken", ""))
    save_obs(obs)
    cmd_build(args)
    dest_note = f" into {LOCAL_OBS_F.name} (local only, never published)" if args.local else ""
    print(f"\nAdded {added} new photo(s){dest_note}, skipped {skipped} already in the library.")
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
    # Tell the app where each thumbnail actually lives, so it doesn't have to know
    # the tracked/local split. publish() overwrites this with the published layout.
    for o in obs:
        o["thumb"] = f"{thumb_dir(o).name}/{o['file']}"
    DATA_JS.parent.mkdir(parents=True, exist_ok=True)
    payload = {"species": species, "observations": obs,
               "generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}
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
        for sid in [o["species_id"]] + o.get("also", []):
            sightings.setdefault(sid, []).append(o)

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
        for o in shots:
            where = f"{o['lat']}, {o['lon']}" if o.get("lat") else "no location"
            sec = " [background]" if o["species_id"] != sid else ""
            print(f"      {o.get('taken','')[:16]}  {where}  {o['file'][:8]}…{sec}")
        print()

    counts = {}
    for _, st, _, _, _ in rows:
        counts[st] = counts.get(st, 0) + 1
    print("Summary: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items(), key=lambda x: RANK.get(x[0], 9))))
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

    for t in sorted(THUMBS.glob("*.jpg")) + sorted(THUMBS_LOCAL.glob("*.jpg")):
        head = t.read_bytes()[:8192]
        if b"Exif" in head or b"http://ns.adobe.com/xap" in head:
            problems.append(f"{t.relative_to(ROOT)} still has an EXIF/XMP segment")

    obs = load_obs()
    missing = [o["file"] for o in obs if not o.get("lat")]
    imprecise = [o["file"] for o in obs
                 if o.get("lat") and len(o["lat"].split(".")[-1]) < 4]
    for f in missing:
        warnings.append(f"{f} has no coordinates — of limited survey value")
    for f in imprecise:
        problems.append(f"{f} has a coordinate rounded below survey precision")

    if problems:
        print("CHECK FAILED:")
        for p in problems[:10]:
            print(f"  ! {p}")
        if len(problems) > 10:
            print(f"  ... and {len(problems) - 10} more")
        sys.exit(1)
    if warnings:
        print(f"{len(warnings)} record(s) without coordinates:")
        for w in warnings[:5]:
            print(f"  - {w}")
        if len(warnings) > 5:
            print(f"  ... and {len(warnings) - 5} more")
    print(f"Check passed — thumbnails carry no EXIF; "
          f"{len(obs) - len(missing)}/{len(obs)} records have survey-grade coordinates.")


def cmd_publish(args):
    """Assemble a public/ folder: app + thumbnails + survey data.

    Coordinates are published at full precision — see the note at the top of this
    file. Screened-out photos are withheld entirely.
    """
    cmd_build(args)
    pub = ROOT / "public"
    if pub.exists():
        shutil.rmtree(pub)
    (pub / "app").mkdir(parents=True)
    shutil.copy2(ROOT / "index.html", pub / "index.html")

    # Built from the source files, NOT from app/data.js — that file deliberately
    # merges in local-only records, and reading it back would republish them.
    # Screened-out photos are excluded too: by definition a rejected photo isn't
    # vegetation (someone's camera roll spilling in), so neither the record nor its
    # thumbnail belongs on a public site.
    kept = public_obs()
    dropped = len(load(OBS_F, [])) - len(kept)
    withheld = len(load(LOCAL_OBS_F, []))
    for o in kept:
        o.pop("hash", None)
        o["thumb"] = f"thumbs/{o['file']}"   # public/ has one flat thumbs dir
    payload = {"species": enriched_species(), "observations": kept,
               "generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}
    (pub / "app" / "data.js").write_text("window.PLANT_DB = " + json.dumps(payload, indent=1) + ";\n")

    # Copy only the thumbnails the published data actually references — that way a
    # rejected (or deleted) observation can't leave an orphan image behind.
    (pub / "thumbs").mkdir(parents=True, exist_ok=True)
    n = 0
    for o in kept:
        t = THUMBS / o["file"]
        if t.exists():
            shutil.copy2(t, pub / "thumbs" / t.name)
            n += 1
    (pub / ".nojekyll").touch()
    size = sum(f.stat().st_size for f in pub.rglob("*") if f.is_file()) / 1e6
    print(f"Built public/ — {len(payload['species'])} species, {n} thumbnails, {size:.1f} MB.")
    if dropped:
        print(f"Withheld {dropped} screened-out photo(s) — not published.")
    if withheld:
        print(f"Withheld {withheld} local-only record(s) from {LOCAL_OBS_F.name} — not published.")
    print("Full-resolution originals stay local in photos/ and are never published.")


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
    sub.add_parser("species", help="list species ids").set_defaults(func=cmd_species)
    sub.add_parser("todo", help="list photos not yet identified").set_defaults(func=cmd_todo)
    sub.add_parser("publish", help="build public/ with GPS scrubbed, ready to deploy").set_defaults(func=cmd_publish)
    sub.add_parser("scrub", help="strip EXIF from thumbnails and blur tracked coordinates").set_defaults(func=cmd_scrub)
    sub.add_parser("verify", help="check nothing tracked carries precise location data").set_defaults(func=cmd_verify)
    inv = sub.add_parser("invasives", help="survey report: non-native species with dates and locations")
    inv.add_argument("--all", action="store_true", help="include native species too")
    inv.set_defaults(func=cmd_invasives)
    a = ap.parse_args()
    a.func(a)
