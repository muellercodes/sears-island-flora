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

## Field verification

The premise of this survey is that a machine identification is a lead, not a finding.
That only means anything if the confirmation is recorded, so it is a first-class field:

```bash
# a person went and looked, and the AI was right
python3 scripts/plantdb.py confirm --file IMG_1234.jpg --by "J. Whitten" \
    --status confirmed --notes "Two mature shrubs, hollow pith."

# a person went and looked, and it is something else
python3 scripts/plantdb.py confirm --file IMG_1234.jpg --by "J. Whitten" \
    --status corrected --species dogbane --notes "Opposite leaves, milky sap."

# what still needs checking, most urgent first
python3 scripts/plantdb.py unverified --status regulated
```

`--status` is one of `confirmed`, `corrected`, `rejected`, `revisit`.

**The model's answer and the human's answer are stored separately and never
overwrite each other.** `species_id` stays whatever the pipeline decided;
everything under `verified` is written only by `confirm`. The site groups records
by what is currently believed — the human verdict where there is one — while still
showing what the model originally said.

That separation is deliberate beyond tidiness: it means an outside editor (a shared
spreadsheet for the Friends of Sears Island, say) can own the verification columns
outright without ever colliding with the pipeline, because no field has two writers.

Unverified records are marked as such everywhere they appear, and their map pins are
drawn with an open, dashed ring — the ring is the claim, and it is not closed yet.

## Photos from a shared Drive folder

Contributors drop photos in a shared Google Drive folder; the pipeline reads that
folder and nothing else. This uses the same service account as the review sheet, so
there is no desktop app and no local mirror — and it runs headlessly, which matters
the day this moves off a laptop.

**Enable the Drive API** for the project (separate from Sheets):
<https://console.cloud.google.com/apis/library/drive.googleapis.com>

Share the folder with the service account's `client_email`, then find its id:

```bash
python3 scripts/plantdb.py drive-folders     # lists what the service account can see
```

Put it in `.env` as `GOOGLE_DRIVE_FOLDER_ID`, then:

```bash
python3 scripts/plantdb.py ingest-drive --limit 5    # try a few first
python3 scripts/plantdb.py ingest-drive              # the rest
```

Only images directly in the folder are read — subfolders are not walked, because a
flat drop-box is easier for contributors to get right. Downloaded Drive file ids are
remembered so bytes are never re-fetched; that is separate from the content-hash
dedupe, which stops the same photo being added twice even under a new name. A file id
is recorded only after the bytes are on disk, so a failed download retries next run.

## Steward review in a Google Sheet

Field verification does not need a developer. A shared sheet gives the Friends of
Sears Island a place to confirm, correct, and annotate records with the photo right
there in the row.

**This is not a general two-way sync, deliberately.** Every column has exactly one
writer:

| Columns | Owner | Direction |
|---|---|---|
| file, photo, photographed, latitude, longitude, AI identification, AI confidence, AI notes | pipeline | push → sheet |
| STATUS, corrected species, verified by, verified date, field notes | **a person** | sheet → pull |

No field has two writers, so there is no merge and nothing to resolve. A steward can
be editing while a batch run identifies new photos, and neither clobbers the other.
Push reads the human columns and writes them straight back untouched, so it can
never blank someone's work.

### One-time setup

1. **Create a Google Cloud project** — <https://console.cloud.google.com/projectcreate>.
   Any name; it exists only to hold the credential.
2. **Enable the Sheets API** for that project —
   <https://console.cloud.google.com/apis/library/sheets.googleapis.com> → *Enable*.
3. **Create a service account** — *IAM & Admin → Service Accounts → Create*. No roles
   are needed; access comes from sharing the sheet, not from project roles.
4. **Make a JSON key** — open the service account → *Keys → Add key → Create new key
   → JSON*. It downloads once. Store it outside the repo.
5. **Create the sheet**, then **share it with the service account's email**
   (`something@your-project.iam.gserviceaccount.com`) as **Editor**. This step is what
   grants access — without it every call returns 404, which looks like a wrong id.
6. **Add both values to `.env`:**

   ```bash
   GOOGLE_SERVICE_ACCOUNT_JSON=/Users/you/.config/sears-island/service-account.json
   GOOGLE_SHEET_ID=<the long id from the sheet URL, between /d/ and /edit>
   ```

### Day to day

```bash
python3 scripts/plantdb.py sheet-push        # publish records for review
python3 scripts/plantdb.py sheet-pull        # preview what stewards changed
python3 scripts/plantdb.py sheet-pull --yes  # apply it
```

`sheet-push` creates the tab, freezes the header, adds a dropdown on STATUS, sets the
row height so thumbnails are readable, and marks the pipeline columns
warning-protected — they are overwritten on the next push, so an edit there is
silently lost work.

`sheet-pull` refuses anything it cannot trust and says why: an unknown status, a
`corrected` row with no species, an unknown species id, a row for a record that does
not exist, or — importantly — a verification with nobody's name against it. An
unattributed verification is not a verification.

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

That argument holds **only for the survey area.** It does not transfer to photos taken
somewhere people live, which is what the upstream project was protecting against. For
anything outside Sears Island — test batches especially — use `--local`:

```bash
python3 scripts/plantdb.py ingest ~/some-folder --local
```

Local-only records go to `data/observations-local.json` and their thumbnails to
`thumbs-local/`. Both are gitignored, so they cannot be committed or published even by
`autopilot.sh`'s `git add -A`. They still appear in the local app, map included. The
file a record lives in *is* the marker — there is no flag to forget to set.

`app/data.js` is generated and gitignored for the same reason: `build` merges local-only
records into it so the local map can show them. The deploy workflow regenerates it, and
`publish` writes `public/` from the source JSON rather than reading that file back.

## Image hosting

Thumbnails can either be bundled into the published site or served from Cloudflare R2.
Bundling is the default and is fine for a few hundred photos; past that, git history
becomes the problem, because it is permanent — deleting a photo later does not shrink
the repo, and a survey heading for thousands of images would push it past GitHub's
recommended 1 GB and the Pages site limit.

R2 is the escape hatch: 10 GB free, and unlike S3 the egress is free, which is the
whole point for serving images.

**Setup.** Create a bucket, enable public access on it, and make an API token scoped to
*Object Read & Write* for that bucket. Then put the credentials in `.env` (gitignored,
and already sourced by `autopilot.sh`):

```bash
R2_ACCOUNT_ID=...
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...
R2_BUCKET=sears-island-flora
```

Check them before a real run — this uploads a single tiny object and reports:

```bash
python3 scripts/r2.py
```

Then set the public base URL in `data/publish-config.json` (the r2.dev subdomain from
the bucket's public-access settings, or a custom domain):

```json
{ "r2_public_base": "https://pub-xxxxxxxx.r2.dev", "r2_prefix": "thumbs" }
```

Once that is set, `publish` uploads any thumbnail R2 doesn't already have and writes
absolute URLs into the site. A gitignored `data/r2-manifest.json` records what has been
uploaded, keyed by content hash, so re-publishing doesn't re-send thousands of unchanged
images.

**Then make the cutover** — add `thumbs/` to `.gitignore` and `git rm -r --cached thumbs`.
That is the step that actually keeps the repo small; until you take it, images are in
git *and* on R2.

The split of responsibilities is deliberate: the public base URL is not a secret and is
tracked, so the deploy runner can build correct image URLs, while credentials stay local
and uploads happen on the machine that ingested the photos. The runner publishes with
`--no-upload` and never holds a credential. If an upload fails, `publish` stops before
writing anything, so the site never goes out referencing images that aren't there.

## Map

The **Map** view plots every located record on OpenStreetMap tiles. Markers are the
photo itself, ringed in its regulatory-status colour; tap one for a card, or *Full
details* for the species sheet. Pins that would overlap merge into one with a count and
separate as you zoom — grouped by distance on screen rather than by rounded coordinates,
so nothing is hidden underneath and no precision is thrown away. A mixed pin takes the
colour of its most urgent member, so a regulated find is never hidden behind a native
one beside it.

Leaflet and the tiles are this app's only external dependency and load lazily when the
Map view is first opened. The Species and Photos views stay entirely self-contained, and
still work with no network.

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

### Batch mode — half price

Identification is not latency-sensitive: a survey does not care whether an answer
lands in four seconds or four hours. The Batch API halves the price for exactly that
trade, and combines with prompt caching.

```bash
.venv/bin/python scripts/identify.py --batch              # submit, wait, apply
.venv/bin/python scripts/identify.py --batch --no-wait    # submit and walk away
.venv/bin/python scripts/identify.py --collect            # apply results later
```

Submitted batch ids are recorded in the cache database, so an interrupted wait is
resumed with `--collect` rather than resubmitted — a batch may take up to 24 hours,
and losing the id would mean paying twice and never collecting the first run.

Requests are chunked to stay under the 256 MB per-batch limit (a base64 thumbnail is
~270 KB, so this matters), and every result is matched by `custom_id` — the photo's
content hash — because batch results come back in arbitrary order.

Use the synchronous path when you want to watch the first few land; use `--batch`
for anything bigger.

### Never paying twice for the same photo

Three layers, all verifiable with `plantdb.py doctor` and the commands below:

1. **Ingest** skips any photo already in the library, matched by content hash — the
   same image from a different folder or filename is not re-added.
2. **Identify** only selects records still marked `species_id: unknown`, so anything
   already identified is never sent again.
3. **A retry cap** (`--max-attempts`, default 2) stops re-paying for photos the model
   genuinely cannot identify. Those stay `unknown` forever, so without the cap every
   `--all-unknown` run would bill for them again. `--retry-exhausted` overrides it.

Each attempt is counted in `id_attempts` *before* the API call, so a crash or timeout
still counts — the cap holds even when a run dies mid-flight.

```bash
python3 scripts/plantdb.py ingest DIR   # copy in, thumbnail, strip EXIF, keep precise GPS
python3 scripts/plantdb.py ingest DIR --local   # ...but never commit or publish these
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

## Is it ready?

```bash
python3 scripts/plantdb.py doctor
```

Reports what is configured and what still blocks a real run — venv, API key, R2
credentials, survey area, leftover stand-in data, watcher. Exits non-zero while
anything is outstanding.

### Where the API key lives

`ANTHROPIC_API_KEY` is used by `identify.py` only, which runs on your machine at
ingest time. **The published site never uses it.** The site is static HTML, JS and
JSON; identification has already happened by the time anything is published, so
there is no key in the browser, none in the repo, and none in GitHub Actions. The
deploy workflow only rebuilds HTML from committed JSON.

The key lives in `.env`, which is gitignored and sourced by `autopilot.sh`.

## Survey area

`survey_area` in `data/publish-config.json` bounds where survey records may come from.
`verify` checks every published record against it, and `ingest` warns immediately if a
batch lands outside — while undoing it is still one command, rather than after the batch
has been identified and uploaded.

Enforcement is automatic rather than a switch to remember:

| `enforce` | Behaviour |
|---|---|
| `"auto"` (default) | Advisory while no in-area record exists — the published set is still stand-in data. Becomes binding the moment the first genuine Sears Island record is published. |
| `true` | Always binding. |
| `false` | Off. |

The point of `auto` is that the arrival of real island photographs is what forces the
placeholder data out. `verify` runs before `publish` in the deploy workflow, so a mixed
set fails the build and names the `remove` command to fix it.

The bounds shipped are a deliberately generous box around the island, its causeway and
immediate shoreline. Tighten or widen them as you learn the ground — they are
approximate, not surveyed.

## Retiring the proof-of-concept data

The published records are currently a stand-in batch photographed in Orono, not on
Sears Island. The site says so in a banner driven by the `notice` field in
`data/publish-config.json`.

When real island photographs replace them:

```bash
python3 scripts/plantdb.py remove --batch orono --yes   # records, thumbnails, R2 objects
```

Then delete `notice` from `data/publish-config.json`, `publish`, and push. Deleting
the records alone would strand their images on R2 indefinitely, which is why `remove`
handles all three. Originals in `photos/` are left alone.

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
