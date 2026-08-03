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
import argparse, hashlib, json, os, pathlib, re, shutil, subprocess, sys, datetime

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


# A record carries two identifications: what the model said, and what a person
# confirmed on the ground. They are kept in separate fields on purpose — the
# pipeline owns species_id, a human owns everything under `verified`, and neither
# overwrites the other. That is what makes a two-way sync with an outside editor
# (a shared spreadsheet, say) conflict-free later: every field has one writer.
VERIFY_STATUS = ("confirmed", "corrected", "rejected", "revisit")

# How many verifications a single `sheet-pull` may withdraw before it stops and
# asks. The pull runs unattended on a schedule, and an emptied STATUS column looks
# exactly like a steward retracting everything.
MAX_UNATTENDED_CLEARS = 2


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


def is_publishable(o):
    """Does this record say anything? A survey record with no identification doesn't.

    A habitat shot, a bark close-up, a canopy against the sky — the screener is
    right to accept them as vegetation photographs, but if nothing in one could be
    named then it contributes no finding, and on the map it is an anonymous pin
    that dilutes the ones that mean something. Withheld from the published set;
    kept in the data, and in `todo`, because the photo still exists and a better
    one from the same spot may settle it.

    Deliberately derived rather than stored as a flag, so it corrects itself: the
    moment a re-run identifies the photo, or a person records a field verdict on
    it, it publishes again with no bookkeeping to remember.
    """
    if o.get("rejected"):
        return False                       # not a vegetation photograph at all
    if is_verified(o):
        return True                        # a person went and looked; that is a finding
    if o.get("also"):
        return True                        # something in the frame was identified
    return o.get("species_id", "unknown") != "unknown"


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
    else:
        taken = datetime.datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S +0000")
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
        # Both copies are full precision here — see PRIVATE_F above. The sidecar is
        # kept only so a record's original coordinates survive an edit to
        # observations.json; it is not a privacy boundary in this fork.
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
    # Tell the app where each thumbnail actually lives, so it doesn't have to know
    # the tracked/local split. publish() overwrites this with the published layout.
    for o in obs:
        o["thumb"] = f"{thumb_dir(o).name}/{o['file']}"
    DATA_JS.parent.mkdir(parents=True, exist_ok=True)
    payload = {"species": species, "observations": obs,
               "generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}
    notice = load(PUBCFG_F, {}).get("notice")
    if notice:
        payload["notice"] = notice
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
            sec = " [background]" if effective_species(o) != sid else ""
            v = o.get("verified") or {}
            mark = f"  ✓ {v['status']} by {v.get('by','?')} {v.get('date','')}" if is_verified(o) \
                   else "  · UNVERIFIED"
            print(f"      {o.get('taken','')[:16]}  {where}  {o['file'][:8]}…{sec}{mark}")
        print()

    counts = {}
    for _, st, _, _, _ in rows:
        counts[st] = counts.get(st, 0) + 1
    print("Summary: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items(), key=lambda x: RANK.get(x[0], 9))))
    nv = sum(1 for _, _, _, shots, _ in rows for o in shots if not is_verified(o))
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
    allpub = load(OBS_F, [])
    screened = sum(1 for o in allpub if o.get("rejected"))
    unidentified = len(allpub) - len(kept) - screened
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
    payload = {"species": enriched_species(), "observations": kept,
               "generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}
    if cfg.get("notice"):
        payload["notice"] = cfg["notice"]
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
    if screened:
        print(f"Withheld {screened} screened-out photo(s) — not vegetation.")
    if unidentified:
        print(f"Withheld {unidentified} photo(s) that could not be identified — nothing "
              f"in them was named, so they are not survey records. Still in `todo`.")
    if withheld:
        print(f"Withheld {withheld} local-only record(s) from {LOCAL_OBS_F.name} — not published.")
    print("Full-resolution originals stay local in photos/ and are never published.")


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
             "date": args.date or datetime.date.today().isoformat()}
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
    obs = [o for o in load_obs() if not is_verified(o)]
    species = {s["id"]: s for s in enriched_species()}
    rows = []
    for o in obs:
        sp = species.get(effective_species(o), {})
        rows.append((RANK.get(sp.get("origin_status", "unknown"), 9), o, sp))
    rows.sort(key=lambda r: (r[0], r[1].get("taken", "")))
    if args.status:
        rows = [r for r in rows if r[2].get("origin_status") == args.status]
    if not rows:
        print("Everything is field-verified.")
        return
    print(f"{len(rows)} record(s) awaiting field verification:\n")
    cur = None
    for rank, o, sp in rows[: args.limit or len(rows)]:
        st = sp.get("origin_status", "unknown")
        if st != cur:
            cur = st
            print(f"\n=== {st.upper()} ===")
        print(f"  {o['file']}")
        print(f"    {sp.get('common', o.get('species_id'))}  [{o.get('confidence','?')}]"
              f"  {o.get('lat','')}, {o.get('lon','')}")
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


def _sheets():
    require("googleapiclient", "google-api-python-client google-auth")
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import sheets
    cfg = sheets.config()
    if not cfg:
        sys.exit("Google Sheet not configured. Missing: " + ", ".join(sheets.missing_vars())
                 + "\nSee 'Steward review in a Google Sheet' in the README.")
    return sheets, cfg


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
    got = 0
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
        # Recorded only after the bytes are on disk, so a failed download is retried
        # on the next run rather than silently skipped forever.
        idcache.drive_record(con, f["id"], f["name"])
        got += 1
        print(f"  + {f['name']}")
    print(f"\nDownloaded {got} file(s) to {staging.name}/")
    if got:
        args.folder = str(staging)
        args.batch = args.batch or datetime.date.today().isoformat()
        cmd_ingest(args)
    shutil.rmtree(staging, ignore_errors=True)


def verification_problem(v, species_ids):
    """Why these human columns are not an acceptable verification, or None.

    One definition, used by both directions: the pull decides what to apply with it,
    and the push writes the reason back into the sheet's "recorded?" column with it.
    If those two ever disagreed, the sheet would tell a steward their row was fine
    while the pipeline quietly dropped it — which is the failure this whole column
    exists to prevent.
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


def cmd_sheet_push(args):
    """Send the machine columns to the sheet. Never touches the human columns."""
    sheets, cfg = _sheets()
    svc = sheets.service(cfg)
    # Everything except what the screener threw out. Unidentified photos DO go to
    # the sheet — a steward who knows the flora can name one, and `corrected` is
    # exactly the route back for a record the pipeline gave up on. A screened-out
    # photo is different: it is not vegetation, so there is nothing to review, and
    # a verdict on it could not change anything (it stays withheld either way).
    obs = [o for o in load_obs() if not o.get("rejected")]
    species = {s["id"]: s for s in enriched_species()}
    base = (load(PUBCFG_F, {}).get("r2_public_base") or "").rstrip("/")
    if base:
        base += "/" + (load(PUBCFG_F, {}).get("r2_prefix") or "thumbs")
    # Read the human columns first and write them straight back, so a push can never
    # blank a steward's work — even one made between this read and the write.
    existing = sheets.pull(svc, cfg)

    # Tell each steward whether their row actually landed. A refused verification
    # otherwise only exists as a line in a CI log nobody opens, and the person who
    # walked out there is left believing it was recorded.
    ids = set(species)
    feedback, refused = {}, 0
    for o in obs:
        v = existing.get(o["file"], {})
        problem = verification_problem(v, ids)
        if problem:
            feedback[o["file"]] = f"⚠ not recorded — {problem}"
            refused += 1
        elif not (v.get("status") or "").strip():
            feedback[o["file"]] = ""
        elif o.get("verified"):
            feedback[o["file"]] = f"✓ recorded {o['verified'].get('date', '')}".strip()
        else:
            feedback[o["file"]] = "… will be recorded on the next sync"

    n_sp = sheets.push_species(svc, cfg, list(species.values()))
    n = sheets.push(svc, cfg, obs, species, base, existing, feedback)
    print(f"Pushed {n} record(s) to the sheet, and {n_sp} species to the "
          f"'{sheets.SPECIES_TAB}' tab for the corrected-species dropdown.")
    if refused:
        print(f"  ! {refused} row(s) have a verification the sync cannot accept. "
              f"The reason is now in each row's 'recorded?' column.")
    print(f"  https://docs.google.com/spreadsheets/d/{cfg['sheet_id']}/edit")
    if not base:
        print("  (no r2_public_base set — the photo column will be empty)")


def cmd_sheet_pull(args):
    """Read steward verifications back. Previews by default; --yes to apply."""
    sheets, cfg = _sheets()
    svc = sheets.service(cfg)
    rows = sheets.pull(svc, cfg)
    obs = load_obs()
    by_file = {o["file"]: o for o in obs}
    ids = {s["id"] for s in load(SPECIES_F, [])}

    changes, problems = [], []
    for f, v in rows.items():
        o = by_file.get(f)
        if o is None:
            problems.append(f"{f}: no such record (row ignored)")
            continue
        cur = o.get("verified") or {}
        if not v["status"]:
            if cur:
                changes.append((o, None, f"clear verification (was {cur.get('status')})"))
            continue
        # Same rules the push writes into the sheet's "recorded?" column, so a
        # steward is never told their row is fine while this quietly drops it.
        if (problem := verification_problem(v, ids)):
            problems.append(f"{f}: {problem}")
            continue
        new = {"status": v["status"], "by": v["by"],
               "date": v["date"] or datetime.date.today().isoformat()}
        if v["species_id"]:
            new["species_id"] = v["species_id"]
        if v["notes"]:
            new["notes"] = v["notes"]
        if new != cur:
            changes.append((o, new, f"{cur.get('status', '—')} -> {v['status']} by {v['by']}"))

    for p in problems:
        print(f"  ! {p}")
    if not changes:
        print("No verification changes in the sheet." if not problems else "\nNo applicable changes.")
        return

    # Clearing a verification is the one destructive thing a pull can do, and it is
    # indistinguishable from an accident: select the STATUS column, press delete,
    # and every field check ever recorded is withdrawn on the next hourly run. A
    # steward changing their mind about one or two records is ordinary; a dozen at
    # once is a mis-click. Withdrawals are the hardest data here to reconstruct —
    # they represent someone having walked out there — so past a small number this
    # stops and waits for a person.
    clears = [c for c in changes if c[1] is None]
    if len(clears) > MAX_UNATTENDED_CLEARS and not args.force:
        print(f"\n  ! {len(clears)} verification(s) would be WITHDRAWN in one pull:")
        for o, _, desc in clears[:10]:
            print(f"      {o['file'][:14]}…  {desc}")
        if len(clears) > 10:
            print(f"      ... and {len(clears) - 10} more")
        print(f"\n    More than {MAX_UNATTENDED_CLEARS} at once usually means the STATUS column")
        print("    was cleared or shifted by accident, not that this many people changed")
        print("    their mind. Nothing was applied — not even the other changes.")
        print("    Check the sheet, then re-run with --force if it is genuinely right.")
        sys.exit(1)

    print(f"\n{len(changes)} change(s) from the sheet:")
    for o, new, desc in changes[:20]:
        print(f"  {o['file'][:14]}…  {desc}")
    if len(changes) > 20:
        print(f"  ... and {len(changes) - 20} more")
    if not args.yes:
        print("\nNothing changed. Re-run with --yes to apply.")
        return
    for o, new, _ in changes:
        if new is None:
            o.pop("verified", None)
        else:
            o["verified"] = new
    save_obs(obs)
    cmd_build(args)
    print(f"\nApplied {len(changes)} verification change(s).")


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
    for bid, created, n, model, region in opens:
        print(f"  {bid}")
        print(f"      {n} photo(s), {model}, submitted {created}")
    print("\nCollect them with:  .venv/bin/python scripts/identify.py --collect")
    print("A batch may take up to 24 hours; results are kept for 29 days.")


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

    if venv.exists():
        ok_g = subprocess.run([str(venv), "-c", "import googleapiclient"],
                              capture_output=True).returncode == 0
        check(ok_g, "Google Sheets client installed",
              "Sheets client missing — .venv/bin/pip install google-api-python-client google-auth")
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    try:
        import sheets as _sh
        check(bool(_sh.config()), "Google Sheet configured",
              "Google Sheet not configured (" + ", ".join(_sh.missing_vars())
              + ") — optional; enables steward review")
    except ImportError:
        pass

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
    print("Originals in photos/ are NOT touched — re-ingest to bring them back.")
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

    gone = 0
    for o in sel:
        if not o.get("local_only") and (creds or bucket):
            key = f"{prefix}/{o['file']}"
            try:
                if creds:
                    r2.delete(creds, key)
                else:
                    subprocess.run(["npx", "wrangler", "r2", "object", "delete",
                                    f"{bucket}/{key}", "--remote"],
                                   capture_output=True, text=True, timeout=120)
                gone += 1
            except Exception as e:
                print(f"  ! could not delete {key}: {e}")
        manifest.pop(o["file"], None)
        thumb_path(o).unlink(missing_ok=True)

    drop = {o["file"] for o in sel}
    save_obs([o for o in obs if o["file"] not in drop])
    save(R2_MANIFEST, manifest)
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
    common = norm_common(sp.get("common"))
    for w in HEDGE_WORDS:
        if re.search(rf"\b{w}", common):
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
        print(f"{len(dropped)} entr(ies) are descriptions, not species:\n")
        for sp, reason, n in dropped:
            print(f"  {sp['id']}")
            print(f"      {sp['common']!r}  ({sp['scientific']})")
            print(f"      {reason}")
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
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler)
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
    sp_ = sub.add_parser("sheet-push", help="send records to the steward review sheet")
    sp_.set_defaults(func=cmd_sheet_push)
    pl = sub.add_parser("sheet-pull", help="read steward verifications back from the sheet")
    pl.add_argument("--yes", action="store_true", help="apply (otherwise just previews)")
    pl.add_argument("--force", action="store_true",
                    help=f"allow withdrawing more than {MAX_UNATTENDED_CLEARS} verifications at once")
    pl.set_defaults(func=cmd_sheet_pull)
    sub.add_parser("cache", help="what we've already paid to identify, and what it cost").set_defaults(func=cmd_cache)
    sub.add_parser("batches", help="batches submitted to the Batch API and not yet collected")\
       .set_defaults(func=cmd_batches)
    sub.add_parser("doctor", help="report what is configured and what still blocks a run").set_defaults(func=cmd_doctor)
    rm = sub.add_parser("remove", help="delete records, their thumbnails and their R2 objects")
    rm.add_argument("--batch", help="every record from this batch")
    rm.add_argument("--file", nargs="*", help="these specific filenames")
    rm.add_argument("--yes", action="store_true", help="actually delete (otherwise just previews)")
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
