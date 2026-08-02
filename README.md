# Sears Island Flora Survey

A volunteer botanical inventory of Sears Island, Searsport, Maine. Contributors
photograph plants; the pipeline identifies them, flags non-native and state-regulated
invasive species, and publishes a searchable, mappable record of what is growing where.

> **Nothing here is a finding until a person has confirmed it on the ground.**
> Identifications are AI-generated from photographs. See [Data quality](#data-quality).

Forked from a family foraging field guide, with the priorities deliberately inverted —
see [What changed from upstream](#what-changed-from-upstream).

---

## Why

Sears Island is roughly 940 acres of largely undeveloped land in Penobscot Bay, and its
future use is contested. Arguments on every side lean on claims about what is actually
out there. A dated, located, photo-backed inventory is more useful than any of those
claims — and invasive infestations are cheapest to treat when they are found small.

## What it does

```
volunteers photograph plants → shared folder
        ↓
   ingest (thumbnail, strip EXIF, keep precise GPS)
        ↓
   screen  → is this a vegetation photo? reject anything else
        ↓
   identify → species, confidence, and Maine regulatory status
        ↓
   publish → searchable site + survey report
```

## Data quality

This is the part that matters most, because the output may be read by people with a
stake in the answer.

- **Every identification is machine-generated from a single photograph.** No record is
  field-verified unless a human has marked it so.
- **The identifier is instructed to under-claim**: identify only to the rank the photo
  supports, return genus when species isn't visible, and flag anything that *might* be
  a regulated invasive even at low confidence. A false positive costs a walk; a false
  negative misses an infestation.
- **`data/invasive-reference.json` is a working copy, not an authority.** It was
  compiled from knowledge of Maine DACF's Do Not Sell list and Advisory List, not
  transcribed from the current published rule. Check it against
  [the live list](https://www.maine.gov/dacf/php/horticulture/invasiveplants.shtml)
  before reporting anything to an agency.
- **Genus-only records are marked `unknown` status**, not guessed, when the genus holds
  both native and introduced species. On the seed catalogue that's 10 of 41 — an honest
  number, and each one is a "go back and look" task.

## Screening

Contributor photos are screened before entering the survey. The check is an
**allowlist**, not a blocklist: *is this a photograph of plants, fungi, or vegetated
landscape?* Anything else — screenshots, documents, indoor scenes, people as a subject,
animals — is rejected with a reason and never reaches the catalogue.

Asking what an image *is* fails safer than trying to enumerate what it must not be, and
it catches the realistic common case: someone's camera roll spilling in alongside the
plant photos. A person incidentally in the background of a valid vegetation photo is
accepted and noted.

Rejected records stay in `data/observations.json` marked `rejected: true` so the
pipeline doesn't re-process them; their images are not published.

## Location precision

**Coordinates are kept at full precision and published.** This is the opposite of the
upstream project, and it is deliberate: the island is uninhabited public land, and a
sighting located to within a kilometre cannot be acted on. `verify` enforces this
direction — it fails on coordinates that have been rounded below survey precision.

EXIF is still stripped from published thumbnails. The useful GPS is extracted into the
JSON; leaving EXIF in the image only exposes contributor camera serials and device
identifiers for no benefit.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install anthropic
echo 'ANTHROPIC_API_KEY=sk-ant-...' > .env

python3 scripts/plantdb.py ingest ~/Dropbox/sears-island-photos
.venv/bin/python scripts/identify.py --limit 5     # start small
python3 scripts/plantdb.py invasives               # the survey report
python3 scripts/plantdb.py publish
```

Automate it once you trust the output:

```bash
./scripts/install-watcher.sh ~/Dropbox/sears-island-photos
```

To publish:

```bash
gh repo create sears-island-flora --public --source=. --remote=origin --push
gh api -X POST repos/OWNER/sears-island-flora/pages -f build_type=workflow
```

## Commands

```bash
python3 scripts/plantdb.py ingest DIR   # copy in, thumbnail, strip EXIF, keep precise GPS
python3 scripts/plantdb.py invasives    # survey report by regulatory status, with locations
python3 scripts/plantdb.py invasives --all   # include natives
python3 scripts/plantdb.py verify       # data-quality check
python3 scripts/plantdb.py publish      # build public/
python3 scripts/plantdb.py todo         # unidentified photos

.venv/bin/python scripts/identify.py --limit 5
```

## What changed from upstream

Forked from a family foraging guide. Four inversions, each for a reason:

| | Upstream (foraging guide) | Here (survey) |
|---|---|---|
| **Coordinates** | Blurred to ~1 km to protect a child's routes | Full precision — location is the deliverable |
| **`verify`** | Fails on precise coordinates | Fails on imprecise ones |
| **Primary axis** | Edibility | Maine regulatory status |
| **Warning** | "Don't eat this without an adult" | "Not a finding until field-verified" |

Foraging notes are retained as secondary detail — they're accurate and occasionally
useful — but they are not what this site is for.

## Seed catalogue

Ships with 41 species from the upstream project (an inland Maine roadside walk),
classified by status: 19 native, 10 undetermined, 8 introduced, 3 invasive, 1 regulated.

Treat it as a starting vocabulary, not a baseline. Coastal island flora differs
substantially from an inland roadside, and identification quality on this catalogue
does not guarantee quality on Sears Island. **Test with real island photos before
trusting the pipeline** — see the watchlist in `data/invasive-reference.json` for the
species most worth hunting there: knotweed, Oriental bittersweet, black swallow-wort,
glossy buckthorn, and *Phragmites* in the marsh edges.

## License

MIT for the code. Survey records are contributed observations; the reference list is a
working compilation and not an authority.
