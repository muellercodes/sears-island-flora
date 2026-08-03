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
- **A description is not a species.** "Fern (unidentified colony)", "Unidentified mature
  hardwood (bark only)" — these name a photograph, not an organism, and once one is in
  the catalogue it is offered to the model as a match for every photo that follows.
  The identifier refuses to create them and `reconcile` removes any that got in; the
  photo stays unidentified, which is the true answer. See
  [Reconciling the catalogue](#reconciling-the-catalogue).
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
| recorded? | pipeline | push → sheet |

### Telling a steward whether their row landed

`recorded?` is the last column and closes the loop. A refused verification otherwise
exists only as a line in a CI log nobody opens, and the person who walked out there
is left believing it was recorded.

| It says | It means |
|---|---|
| *(blank)* | nothing entered yet |
| `✓ recorded 2026-08-02` | it is in the survey |
| `… will be recorded on the next sync` | valid, entered since the last pull |
| `⚠ not recorded — needs a name in 'verified by'` | refused, with the reason. Fix the row and it syncs next run. |

The rule that decides this is the same function the pull uses to decide what to
apply (`verification_problem`), so the sheet can never tell someone their row is
fine while the pipeline drops it.

Two more things make the sheet usable by someone who has never read this file:

- **A `Species` tab**, pushed with the records, listing every id with its common and
  scientific name. `corrected species` takes an *id* — `japanese-knotweed`, not
  "Japanese knotweed" — which nobody can be expected to guess, and a mistyped id is
  the likeliest reason a real field check gets refused. It is now a dropdown you
  pick from.
- **Notes on every header cell** explaining what the column is for and what each
  STATUS value means.

Both dropdowns are deliberately **non-strict**: strict validation blocks pasting a
column of values, which is exactly what a steward does after a day in the field. A
bad value is caught by the sync and explained in `recorded?` — guarded without being
obstructive.

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

### What happens when the sheet gets mangled

It is a shared spreadsheet, so it will be. The pull runs unattended on a schedule,
which raises the stakes on every one of these.

| What someone does | What happens |
|---|---|
| Types garbage into STATUS | Refused, named in the output. Nothing written. |
| `corrected` with no species, or a species id that doesn't exist | Refused. |
| Fills in a verification but no name | Refused — an unattributed verification is not a verification. |
| Edits a pipeline column (file, coordinates, the AI's answer) | Warning-protected in the sheet; overwritten on the next push. If they change `file`, the row stops matching a record and is ignored. |
| **Deletes rows** | Those records simply aren't read. Existing verifications on them are untouched, and the next push puts the rows back. |
| Adds a row with a made-up filename | Ignored — no such record. |
| **Deletes, inserts or reorders a column** | **Everything stops.** See below. |
| **Empties the STATUS column** | Blocked past two withdrawals. See below. |

**A moved column is the dangerous one.** Every field is read by position — status is
column I because that is where push put it. Shift the columns and each value is
silently read as the field beside it: a steward's name as a species id, notes as a
date. None of the value checks above catch it, because each value still looks
plausible in its new place. So the header is verified before anything is believed:

```
The sheet's columns are not where the pipeline put them, so nothing can be read
from it safely.
     A: expected 'file', found 'file'
  -> D: expected 'latitude', found 'longitude'
  -> E: expected 'longitude', found 'AI identification'
```

Fix it by undoing the change (*File → Version history*), which preserves
verifications. If the human columns are already lost, delete the `Records` tab and
run `sheet-push` — it rebuilds from scratch, and anything not already pulled into
`data/observations.json` is gone.

**A mass withdrawal is the other one.** Select the STATUS column, press delete, and
every field check ever recorded reads as "withdrawn" — applied on the next
unattended pull. One or two people changing their mind is ordinary; a dozen at once
is a mis-click, and this is the hardest data in the project to reconstruct, because
it represents someone having walked out there. Past two, the pull stops and applies
nothing at all:

```bash
python3 scripts/plantdb.py sheet-pull            # preview — always safe
python3 scripts/plantdb.py sheet-pull --yes      # apply
python3 scripts/plantdb.py sheet-pull --yes --force   # ...including >2 withdrawals
```

Underneath all of it, `data/observations.json` is in git, so any verification that
was ever pulled is recoverable from history.

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

### The second filter: a record that says nothing

Passing the screener is not enough to be published. A habitat shot, a bark
close-up, a canopy against the sky — all genuinely vegetation, and the screener is
right to accept them — but if nothing in one could be named, it contributes no
finding. On the map it is an anonymous pin diluting the ones that mean something.

So `publish` withholds any record where nothing was identified: no species, no
`also_visible` species in the frame, and no human field verdict. They stay in
`data/observations.json` and in `todo`, because the photo exists and a better one
from the same spot may settle it.

This is derived, not a stored flag (`is_publishable` in `plantdb.py`), so it
corrects itself — the moment a re-run identifies the photo or a steward records a
verdict on it, it publishes again with nothing to remember.

Withholding a record hides it from the site but leaves its thumbnail hosted on R2,
still reachable by URL. `publish` counts those and prints the cleanup:

```bash
python3 scripts/plantdb.py publish --prune-r2
```

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

## Running it on a schedule

Two ways, and you should pick exactly one — both commit to `data/`, and running
both means two writers racing over the same files. `doctor` warns if both are on.

### In the cloud (`.github/workflows/survey-pipeline.yml`)

Two schedules in one file, because identification goes through the Batch API at
half price and batch results come back over hours, not seconds:

| | When | What |
|---|---|---|
| **submit** | 07:23 UTC daily (≈2:23am ET) | pull steward verifications, ingest new photos from Drive, submit a batch, walk away |
| **collect** | :53 on even hours | apply any batch that has finished, reconcile, publish, deploy |

The Batch API discount is a **flat 50%, not time-of-day pricing** — running at 2am
buys the results all night to land in, not a better rate. A collect tick with no
open batch is one SQLite read and no API call, so running twelve of them a day
costs nothing.

The submit run uses `--batch --no-wait` deliberately. Waiting would hold a runner
for up to 24 hours, and an interrupted wait is exactly how a batch gets lost. The
batch id goes into `data/identifications.db`, which is committed — that commit is
what makes the results recoverable. Every run also writes the outstanding batch ids
into its GitHub job summary, so they survive even if that push fails:

```bash
python3 scripts/plantdb.py batches      # anything submitted and not yet collected?
```

**This workflow holds credentials, and `deploy.yml` still does not.** That split is
the point: the workflow that only rebuilds HTML from committed JSON never sees a
key, so a plain push still cannot leak one. Add these under *Settings → Secrets and
variables → Actions*:

| Secret | Value |
|---|---|
| `ANTHROPIC_API_KEY` | same key as `.env` |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | the **contents** of the JSON key file, not a path |
| `GOOGLE_DRIVE_FOLDER_ID` | the shared inbox folder |
| `GOOGLE_SHEET_ID` | the steward review sheet |
| `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET` | as in `.env` |

Trigger a first run by hand from the Actions tab (*Run workflow*) rather than
waiting for the hour — it is the only way to find out whether a secret is wrong.

**Two consequences worth knowing before you turn it on:**

- **Originals stop accumulating locally.** In the cloud, `photos/` lives only for
  the length of the run. Google Drive becomes the archive of full-resolution
  originals — which it already effectively was, since that is where contributors
  put them. `refresh-gps`, which re-reads coordinates from originals, stays a local
  command and needs a local `photos/`.
- **Don't run the pipeline locally at the same time.** `data/identifications.db` is
  a binary file — if both sides commit to it, git cannot merge the conflict and you
  would resolve it by picking one side and losing the other's paid-for
  identifications. Pull before you do local work. `doctor` warns if both schedulers
  are active.

### No images in git, and how that survives a two-run batch

Nothing image-shaped has ever been committed to this repo — `photos/`, `thumbs/`
and `thumbs-local/` are all gitignored, published images are served from R2, and
the whole history is under 2 MB. Keeping it that way is the point: git history is
permanent, so deleting a photo later does not shrink the repo.

Splitting identification across two runs puts one strain on that. A photo's
thumbnail is only uploaded to R2 once something in it has been identified — so a
photo ingested by the submit run is uploaded by a *collect* run, on a different
machine, hours later. Two things carry it across:

- **`thumbs/` rides between runs in the Actions cache.** Only the submit run writes
  it, since only it brings in new images; saving on every collect tick would store
  the whole directory a dozen times a day against the repo's 10 GB cache budget.
- **`data/r2-manifest.json` is committed** (the one deliberate exception in
  `.gitignore`). It records which images are already hosted, and it is the only
  thing that knows an image exists when the local thumbnail does not. Ignored, it
  would be empty exactly when that matters, and `publish` could not tell "already
  uploaded" from "gone".

So if the cache is ever dropped — GitHub evicts after 7 days unused, and daily runs
keep it warm — photos already identified are unaffected, because their images are
on R2 and the manifest says so. Any photo ingested and not yet identified loses its
thumbnail, and `publish` **refuses to ship** rather than emitting a page of broken
images:

```
! 1 publishable record(s) have no thumbnail locally and none on R2:
      IMG_1234.jpg
  Their images exist nowhere. Re-ingest them, then publish again:
      python3 scripts/plantdb.py ingest-drive
```

### On your laptop

```bash
./scripts/install-watcher.sh ~/Dropbox/sears-island-photos
```

A launchd agent running `autopilot.sh` every 15 minutes. Only runs when the machine
is awake and online. Stop it with
`launchctl unload ~/Library/LaunchAgents/com.mueller.searsisland.plist`.

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

### Reconciling the catalogue

Every request in a batch is built from the catalogue as it stood when the batch was
submitted, so **no request can see an entry created by another request in the same
batch.** Two photos of the same lichen in one batch mint two entries — this is how
the survey ended up with both "Pixie-cup Lichen" and "Pixie Cup Lichen (trumpet
lichen)". No prompt fixes it; the information is not in the request. It is repaired
after collection instead:

```bash
python3 scripts/plantdb.py reconcile        # show what would change
python3 scripts/plantdb.py reconcile --yes  # apply it
```

This runs automatically at the end of `identify.py --batch` and `--collect`. It does
two things:

**Merges near-duplicates.** Entries merge on exact agreement after normalisation —
the same common name, or the same binomial. The surviving entry keeps the *older id*,
because that id may already be a link on the published site, and the *fuller
write-up*, because that is what serves a reader. Records, `also` lists and the
identification cache are all repointed, and the retired id is kept in `merged_from`
so a replayed cached result resolves to the survivor instead of recreating the
duplicate.

A shared genus alone is never enough to merge: *Trifolium pratense* and *Trifolium
repens* are two species, not one written up twice. Those are listed for you to judge.

**Drops descriptions posing as species,** by the two tests in `non_answer_reason` —
a hedge word in the common name, or a scientific name at a rank above genus
(`-aceae`, `-ales`, `-phyta`, …; "Bryophyta sp." is the mosses, all of them). Their
records go back to `unknown` with the model's own note intact, plus a line saying
what was removed and why.

Two things it will not touch, deliberately:

- **Entries you wrote yourself.** Only machine-created entries (`source: "auto (…)"`)
  are ever dropped. The seed catalogue has hedged entries a person put there on
  purpose — "Bolete (unidentified)" — and an unattended run must not quietly delete
  an editorial decision.
- **Anything a person has verified a record against.** That would be deleting the
  target of a field check. It says so and leaves it for you.

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
python3 scripts/plantdb.py batches      # batches submitted and not yet collected
python3 scripts/plantdb.py reconcile    # merge duplicate species, drop non-answers
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
