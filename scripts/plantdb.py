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


# A record carries two identifications: what the model said, and what a person
# confirmed on the ground. They are kept in separate fields on purpose — the
# pipeline owns species_id, a human owns everything under `verified`, and neither
# overwrites the other. That is what makes a two-way sync with an outside editor
# (a shared spreadsheet, say) conflict-free later: every field has one writer.
VERIFY_STATUS = ("confirmed", "corrected", "rejected", "revisit")


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


def public_obs():
    """Only what may be published: public file, minus anything the screener rejected."""
    return [o for o in load(OBS_F, []) if not o.get("rejected")]


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
    where = f"{n} thumbnail(s) on R2" if base else f"{n} thumbnails bundled"
    print(f"Built public/ — {len(payload['species'])} species, {where}, {size:.1f} MB.")
    if dropped:
        print(f"Withheld {dropped} screened-out photo(s) — not published.")
    if withheld:
        print(f"Withheld {withheld} local-only record(s) from {LOCAL_OBS_F.name} — not published.")
    print("Full-resolution originals stay local in photos/ and are never published.")


def upload_thumbs(kept, prefix, args):
    """Push any thumbnail R2 doesn't already have. Returns the number now hosted.

    A manifest of what's been uploaded (keyed by content hash) keeps this cheap on
    re-runs — at survey scale, re-uploading thousands of unchanged images every
    publish would dominate the run.
    """
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import r2

    have = load(R2_MANIFEST, {})
    todo = []
    for o in kept:
        t = THUMBS / o["file"]
        if not t.exists():
            continue
        h = sha(t)
        if have.get(o["file"]) != h:
            todo.append((o["file"], t, h))

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


def _sheets():
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import sheets
    cfg = sheets.config()
    if not cfg:
        sys.exit("Google Sheet not configured. Missing: " + ", ".join(sheets.missing_vars())
                 + "\nSee 'Steward review in a Google Sheet' in the README.")
    return sheets, cfg


def cmd_sheet_push(args):
    """Send the machine columns to the sheet. Never touches the human columns."""
    sheets, cfg = _sheets()
    svc = sheets.service(cfg)
    obs = load_obs()
    species = {s["id"]: s for s in enriched_species()}
    base = (load(PUBCFG_F, {}).get("r2_public_base") or "").rstrip("/")
    if base:
        base += "/" + (load(PUBCFG_F, {}).get("r2_prefix") or "thumbs")
    # Read the human columns first and write them straight back, so a push can never
    # blank a steward's work — even one made between this read and the write.
    existing = sheets.pull(svc, cfg)
    n = sheets.push(svc, cfg, obs, species, base, existing)
    print(f"Pushed {n} record(s) to the sheet.")
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
        if v["status"] not in VERIFY_STATUS:
            problems.append(f"{f}: status '{v['status']}' is not one of {', '.join(VERIFY_STATUS)}")
            continue
        if v["status"] == "corrected" and not v["species_id"]:
            problems.append(f"{f}: 'corrected' needs a corrected species id")
            continue
        if v["species_id"] and v["species_id"] not in ids:
            problems.append(f"{f}: unknown species id '{v['species_id']}'")
            continue
        if not v["by"]:
            problems.append(f"{f}: needs a name in 'verified by' — an unattributed "
                            f"verification is not a verification")
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

    plist = pathlib.Path.home() / "Library/LaunchAgents/com.mueller.searsisland.plist"
    check(plist.exists(), "Watcher installed",
          "Watcher not installed — ./scripts/install-watcher.sh <folder> (optional; manual runs work)")

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
    sp_ = sub.add_parser("sheet-push", help="send records to the steward review sheet")
    sp_.set_defaults(func=cmd_sheet_push)
    pl = sub.add_parser("sheet-pull", help="read steward verifications back from the sheet")
    pl.add_argument("--yes", action="store_true", help="apply (otherwise just previews)")
    pl.set_defaults(func=cmd_sheet_pull)
    sub.add_parser("cache", help="what we've already paid to identify, and what it cost").set_defaults(func=cmd_cache)
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
    sub.add_parser("species", help="list species ids").set_defaults(func=cmd_species)
    sub.add_parser("todo", help="list photos not yet identified").set_defaults(func=cmd_todo)
    pb = sub.add_parser("publish", help="build public/, uploading images to R2 if configured")
    pb.add_argument("--no-upload", action="store_true",
                    help="skip the R2 upload (the deploy runner uses this — it has no credentials)")
    pb.set_defaults(func=cmd_publish)
    sub.add_parser("scrub", help="strip EXIF from thumbnails and blur tracked coordinates").set_defaults(func=cmd_scrub)
    sub.add_parser("verify", help="check nothing tracked carries precise location data").set_defaults(func=cmd_verify)
    inv = sub.add_parser("invasives", help="survey report: non-native species with dates and locations")
    inv.add_argument("--all", action="store_true", help="include native species too")
    inv.set_defaults(func=cmd_invasives)
    a = ap.parse_args()
    a.func(a)
