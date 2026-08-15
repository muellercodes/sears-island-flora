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
- **Origin status is marked `unknown`, not guessed, whenever a photograph cannot settle
  it.** Usually that is a genus-only identification where the genus holds both native
  and introduced species. But it is not only genus-only records, and the exception
  matters because it looks like a bug: *Prunella vulgaris* (Self-heal) is identified
  cleanly to species and is still `unknown`, because the native North American
  subspecies and the introduced European one both occur here and are not separable from
  a photograph. The rank of the identification and the confidence of the origin call are
  different questions. On the seed catalogue 10 of 41 are `unknown` — an honest number,
  and each one is a "go back and look" task.

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

That separation is deliberate beyond tidiness: it is what lets an outside editor own
the verification fields outright without ever colliding with the pipeline, because no
field has two writers. It is also what makes an edit on the website safe — see
[contributor mode](#contributor-mode-signing-in-on-the-site), where a correction is
recorded rather than written over the model's answer.

There are two ways to record one — `confirm` at a terminal and the website — and each
records which it was in `verified.via`. Neither infers anything: no confidence score,
no re-run and no similarity between photographs has ever set one of these fields, and
nothing in the pipeline is permitted to.

Unverified records are marked as such everywhere they appear, and their map pins are
drawn with an open, dashed ring — the ring is the claim, and it is not closed yet.

## Photos from a shared Drive folder

Contributors drop photos in a shared Google Drive folder; the pipeline reads that
folder and nothing else. It runs headlessly, which matters the day this moves off a
laptop.

This is now the *second* way photographs arrive, and the older one. Most contributors
should use [Upload a walk](#uploading-a-walk) on the site instead: it checks each
photograph's location and date before anything is sent, which is the failure this
project has paid for before. Drive stays because it is the lowest-friction thing to
explain to someone who will not sign in.

**Enable the Drive API** for the project:
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

## Contributor mode: signing in on the site

**This replaces the Google Sheet, which has been removed.** A spreadsheet works for
someone at a desk; it is a poor tool for a steward standing in front of the plant, it
cannot take a photograph, and it needed a sync in both directions that could — and
did — silently withdraw field checks. Everything it did now happens on the site,
signed in:

- **record a field check** where the plant is, instead of at a terminal
- **correct an identification**, without overwriting what the model said
- **upload a whole walk** — eighty photographs at once, queued for the nightly run
- **add photographs to a find** that already exists
- **mark a photograph surplus** to the one that represents a find
- **withdraw a record** from the site, reversibly, with a reason
- **add a species** to the catalogue, so there is something to correct *to*

### The site is still static, and the survey still has one writer

This is the part worth understanding before anything else, because it is the
constraint everything else was designed around.

`data/observations.json` has exactly **one** writer: the pipeline. That is the
reason this project has never had a merge conflict in a file three scheduled jobs
touch every night. A web app that wrote to the survey directly would end it.

So contributor mode does not write to the survey. It writes to an **inbox**, and
the pipeline drains that inbox on the schedule it already runs — which is exactly
the shape the Google Sheet already has. A sheet is an inbox a person edits by
hand; a Worker is an inbox a phone edits over HTTPS. Neither is a second writer.

```
 phone in the field                    GitHub Actions, every 2h
        │                                        │
        │ POST (bearer token)                    │ inbox-pull --yes
        ▼                                        ▼
 Cloudflare Worker ──► D1 (queue) ──────► data/observations.json
        │             R2 (originals)         ONE writer, unchanged
        ▼
   receipt: "recorded" / "refused, because…"
```

Everything the Worker accepts is checked **again** by `inbox-pull` before a byte
is written, against the same `verification_problem` the sheet used. That is not
belt-and-braces: the Worker is deployed separately from this repository and can be
changed without review, while the project's central claim is that `verified`
records a real human field check. The rule about who may create one therefore
lives in the tested, version-controlled half of the system.

### Roles

A token carries a person's **name and role**, and the Worker reads both from the
database row the token hashes to — never from anything the browser sends. So
`verified.by` cannot be blank, cannot be someone else, and cannot be typed into a
box. The sheet had to refuse unattributed verifications after the fact; here one
cannot be constructed.

| | contributor | verifier | admin | pipeline |
|---|:---:|:---:|:---:|:---:|
| Add a photograph | ● | ● | ● | |
| Record a field check | | ● | ● | |
| Mark a photograph surplus | | | ● | |
| Overwrite someone else's entry | | | ● | |
| Collect the inbox | | | ● | ● |

`pipeline` is the role the unattended run holds, and collecting is the *only*
thing it may do. The token sitting in Actions secrets cannot record a field check,
mark a photograph surplus or upload anything — it can only carry out what a person
already decided. Given what `verified` is for, the credential that runs every
night without supervision should not be able to manufacture one.

The table lives in `plantdb.py` as `CAPABILITIES`; `worker/index.js` carries a
copy so it can refuse early with a useful message, and a test reads that file and
fails if the two drift apart.

### Marking photographs surplus

Patches get shot five to ten times. Grouping already keeps the *listings* honest,
but every frame is still carried: hosted on R2, shipped in the site's data, shown
in the photo strip.

A mark says: **within this find — this species, this patch — this frame is surplus
to the one representing it.** Scoped that way deliberately, because the same
photograph can be the seventh shot of a willowherb patch and the only record there
is of the bittersweet behind it.

**It is not a perceptual hash, and there is no "find duplicates" button.** Two
frames a difference algorithm calls identical are routinely a habit shot and a
close-up of the one feature that settles the identification. The site shows the
frames of one patch side by side and asks a person.

Nothing is deleted. The original stays in `photos/`, the record stays in
`observations.json` with the mark on it, and the find keeps counting it:

```
6 photographs of one patch at 44.45618, -68.88184 · spread ~2 m
   · 1 further frame marked surplus and not carried
```

Unmark it and the next publish carries it again. A mark is also re-derived rather
than trusted: if a re-identification moves either photograph to a different
species, or `refresh-gps` moves one out of the patch, the mark lapses and `verify`
reports it instead of going on hiding a photograph of something else.

Three refusals are worth knowing, because each is a decision rather than a check:

| Refused | Why |
|---|---|
| Surplus to a frame that is itself surplus | A chain of marks leaves the find with no photograph at all. |
| More than 10 m apart | Beyond that these are two finds, each entitled to its own photographs. |
| **It carries the only record of another species at that spot** | The mark is scoped to one species but drops the whole image. A plant caught behind the subject is a real record of it growing there — for an invasive, possibly the only one there will ever be. |

### Managing entries on the site

Everything the sheet used to do, plus the things it could not.

| Action | Who | What it does |
|---|---|---|
| Record a field check | verifier, admin | Writes `verified` — the project's central claim |
| Correct the species | verifier, admin | `verified.status = corrected`, **never** an overwrite of `species_id` |
| Fix coordinates or a date | admin | Keeps the original in `location_was` / `taken_was` |
| Add a curator's note | admin | Sits *beside* the model's `note`, never over it |
| Withdraw a record | admin | Reversible, needs a reason |
| Add a species | admin | So a correction has something to point at |

**The identification itself is not editable, deliberately.** `species_id`, `note`
and `confidence` stay exactly as the pipeline wrote them however wrong they are.
That is what lets the site show "the model said Willowherb, J. Whitten found
Fireweed", what lets the survey report its own error rate honestly, and what makes
`export-imap` able to name an Observer. An edit that quietly replaced the machine's
answer would make a corrected record indistinguishable from one it got right, and
`edit_problem` refuses it by name rather than silently ignoring it.

Withdrawal is soft, attributed and reversible: the record and its original are
kept, only the published page and image go. A reason is required, because a
withdrawn record is invisible on the site and that string is the only account of
why it left. `plantdb.py withheld` lists everything that is not being carried and
why; `plantdb.py withdraw --restore` puts one back — the site can only offer that
while a record is still published, which by definition a withdrawn one is not.

### Uploading a walk

The realistic contribution is not one photograph, it is eighty from a Saturday
morning. **Upload a walk** takes them all at once, names the walk so they stay one
thing, and queues them for the nightly Batch API run.

Identification happens overnight, so the site says so rather than implying
otherwise, and **Your uploads** shows where each walk got to:

```
Causeway, 14 August — 80 photographs
  68 in the survey
   9 waiting for tonight's identification run
   3 could not be accepted
```

### Every photograph needs its own location and date

A record that cannot say *where* and *when* is not a survey record: nobody can be
sent to check it and it cannot be compared against a later visit. So one is never
accepted — **the upload is refused, rather than stored and quietly withheld.**

The check runs twice. The browser reads each file's EXIF before anything is sent,
so a bad photograph costs a second rather than a day; then the pipeline re-reads it
with Pillow, which is the authoritative reader and the one that decides. Anything
the browser cannot parse is uploaded anyway and judged there — wrongly refusing a
good photograph is the worse error.

This is `check-photos` moved to where the mistake is made. It exists because 83
photographs once reached this survey with their EXIF stripped, invisible until
after they had been ingested, thumbnailed and paid for.

**What actually strips EXIF** is worth being precise about, because the guidance
follows from it:

| Route | Location survives? |
|---|---|
| Desktop, original file off the camera or card | yes |
| iPhone → Share → Options → **Location on** → Save to Files | yes |
| iPhone → Photos picker straight into a web page | often not |
| **Taking a photo inside the browser** | **never** — the page has no location at the shutter |
| Anything through a messaging app, web form or re-export | no, and unrecoverably |

So the page does not offer to take a photograph, and when files are refused it says
what to do differently instead of only what went wrong.

**The one exception is attaching to a find that already exists.** Open the find and
use *Add a photograph of this find*: those inherit that find's coordinates and need
none of their own, because somebody is asserting on purpose that this is another
shot of that patch. They still need their date — a new photograph of an old find is
evidence the plant is *still there*, which is the whole value of it. The record is
stamped `location_source: attached-to-find` with `location_from` naming the
photograph it inherited from, so an inherited location is never mistaken for a
measured one.

### Patchy signal

Sears Island has poor coverage, and the point of this is that it is used standing
in front of the plant. A field check or a surplus mark that cannot be sent is kept
on the device and goes automatically when there is a connection; the header says
how many are waiting, and signing out warns before discarding them.

Photographs are deliberately *not* queued — they are megabytes, `localStorage` is
a few, and a quota error that silently ate someone's photograph would be worse
than saying plainly that this one needs a connection.

### Did it land?

Every submission gets a receipt — `recorded`, or `refused` with the reason in words
a contributor can act on — and the site shows them back. A refusal that exists only
in a CI log leaves the person who walked out there believing it was recorded, which
is the failure this closes.

The site is honest about timing, too. A submission is queued, not applied, so it
says so rather than claiming a change the reader can see has not happened:

> Recorded as field-verified. The survey picks this up on its next run, within
> about two hours.

### Setting it up

Nothing below is required. With no `contributor_endpoint` set, the site is the
read-only survey it has always been, and `doctor` says so rather than complaining.

```bash
cd worker
npm install

npx wrangler d1 create sears-island-contributors    # paste the id into wrangler.jsonc
npx wrangler r2 bucket create sears-island-inbox    # NOT public — see below
npx wrangler d1 execute sears-island-contributors --remote --file schema.sql
npx wrangler deploy
```

**The inbox bucket must not have public access.** It holds full-resolution
originals that still carry their EXIF, and the survey strips EXIF from everything
it publishes precisely so contributor camera serials do not go out. It is also the
archive for photographs that arrived this way: Drive is the archive for anything
dropped there, and in a cloud run `photos/` lives only as long as the runner.

Then mint the first admin — this is a one-time bootstrap, because an admin has to
exist before the admin API can be called:

```bash
python3 scripts/plantdb.py contributor bootstrap --name "Your Name"
```

It prints a `wrangler d1 execute` command and a token. Run the command, put the
token in `.env` as `SIF_ADMIN_TOKEN`, then everyone else is one command:

```bash
python3 scripts/plantdb.py contributor add --name "J. Whitten" --role verifier
python3 scripts/plantdb.py contributor add --name "pipeline"   --role pipeline
python3 scripts/plantdb.py contributor list
python3 scripts/plantdb.py contributor revoke --id 3f9a1c22
```

`add` prints a one-time link — `https://…/#key=…`. Send it to the person; opening
it once signs them in on that device, and the token is moved straight out of the
URL so it does not sit in history or in a screenshot of the address bar. Only the
hash is ever stored, so the token cannot be shown again and the database is not a
key ring even to someone holding it.

Finally, point the site at the Worker and give the pipeline its drain token:

```jsonc
// data/publish-config.json — tracked, not a secret, exactly like r2_public_base
{ "contributor_endpoint": "https://sears-island-contributors.<subdomain>.workers.dev" }
```

```bash
# .env, and the same two as repository Actions secrets
SIF_WORKER_URL=https://sears-island-contributors.<subdomain>.workers.dev
SIF_PIPELINE_TOKEN=sif_...        # the `pipeline` role token, nothing more
SIF_ADMIN_TOKEN=sif_...           # local only — minting and revoking
```

Add `SIF_WORKER_URL` and `SIF_PIPELINE_TOKEN` under *Settings → Secrets and
variables → Actions*. **`deploy.yml` still holds no credentials and must stay that
way** — it only rebuilds HTML from committed JSON, and the contributor endpoint is
a tracked config value precisely so that stays true.

Day to day, the pipeline does this on every tick; by hand it is:

```bash
python3 scripts/plantdb.py inbox-pull          # preview — always safe
python3 scripts/plantdb.py inbox-pull --yes    # apply
```

It previews by default and stops rather than applying more than two withdrawals at
once — withdrawals are the hardest data here to reconstruct, since each represents
somebody having walked out there, and on a phone withdrawing is a single tap.

Everything contributor mode does is also available without it, because a
capability that exists only behind a deployed Worker is how a project ends up
unable to repair its own data when something is down:

```bash
python3 scripts/plantdb.py confirm   --file IMG_1234.jpg --by "J. Whitten" --status confirmed
python3 scripts/plantdb.py redundant --file IMG_1235.jpg --of IMG_1234.jpg --by "J. Whitten"
python3 scripts/plantdb.py redundant --file IMG_1235.jpg --unmark
```

### Why the sheet is gone

Partly because the site does its job better. But also because of what building this
turned up.

`sheet-push` wrote the sheet's own human columns back, so a verification recorded
any other way never appeared in the STATUS column — and `sheet-pull` read a blank
STATUS against a stored verification as *withdraw it*. Every `plantdb.py confirm`
on a record that was in the sheet was therefore retracted by the next unattended
pull, silently, usually within two hours. Nothing was actually lost, because no
verification had been recorded yet — but that is luck, not design.

The shape of the bug is the argument. A two-way sync between a spreadsheet and a
survey has to reconcile "blank" against "absent" on every field, forever, and the
cost of getting it wrong falls on the hardest data here to reconstruct. A one-way
inbox has no such question: a submission either exists or it does not.

Verifications still record `via` (`cli` or `site`), which is what a blank-cell
withdrawal could never distinguish.

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

### The second filter: what counts as a survey record

Passing the screener is not enough to be published. `withheld_reason` in
`plantdb.py` asks three questions, and a record has to answer all of them:

| Withheld when | Why |
|---|---|
| The screener rejected it | Not a photograph of vegetation. |
| **It has no location, or no capture date** | A sighting is a claim that a species was *here*, on *this day*. Without both there is nothing to send anyone to check and nothing to compare against a later visit. |
| Nothing in it could be identified | A habitat shot or a bark close-up is a fair vegetation photograph, but if no organism could be named it contributes no finding and only dilutes the pins that mean something. |

A record survives the third test if a human has field-verified it, or if some other
catalogue species is visible in the frame — a plant caught in the background is
still a real record of it growing at that spot.

Withheld records stay in `data/observations.json` and in `todo`. Nothing is deleted;
the photo exists and a better one from the same spot may settle it.

This is derived, not a stored flag (`is_publishable` in `plantdb.py`), so it
corrects itself — the moment a re-run identifies the photo or a steward records a
verdict on it, it publishes again with nothing to remember.

Withholding a record hides it from the site but leaves its thumbnail hosted on R2,
still reachable by URL. `publish` counts those and prints the cleanup:

```bash
python3 scripts/plantdb.py publish --prune-r2
```

## When a photo arrives with no date or location

Coordinates come from the photograph's EXIF, so a photo that reaches the folder with
its EXIF already stripped has none — and there is nothing the pipeline can do to
recover it. That is not a bug on this side; the metadata was gone before the file
arrived. The usual causes are a photo sent through a messaging app, uploaded via a
web form, or re-exported, all of which strip EXIF by design.

The tell is a photo whose EXIF block is a few dozen bytes instead of several
kilobytes. One record in the current survey is like this
(`0AA7200E-…`): no coordinates, no capture date. `verify` reports it as *of limited
survey value*, which is the honest description — it is a real observation of a real
plant that cannot be placed on the map.

**A missing capture date is left blank, never guessed.** It used to fall back to the
file's modification time, which for anything fetched from Drive is the moment we
downloaded it — so the site printed "Photographed 2026-08-02" over a photograph
whose date nobody knew. A survey that will not invent a species must not invent a
date either.

### Check before you upload

```bash
python3 scripts/plantdb.py check-photos ~/Desktop/sears-export
```

Reports which files still carry a location and a date, and exits non-zero if any
do not. **Export two or three, run this, then upload the rest.**

This exists because of an expensive surprise: 83 photographs reached the survey
with their EXIF stripped, and it was invisible until after they had been ingested,
thumbnailed and paid for. The stripped copies carried a *full-size but empty* EXIF
block, so even the file size looked right.

**On macOS, the metadata is lost on the way out of Photos, not on the way to
Drive.** Dragging out of Photos.app hands you a rendered derivative rather than the
file it is holding, so dragging to a Finder folder first does not help. Use:

> **File → Export → Export Unmodified Original for N Photos…**

Not drag-and-drop. Not plain *Export…* unless *Location Information* is ticked.
The two export paths are distinguishable afterwards in the filename Photos
generates — `..._1_105_c` came through with GPS intact, `..._4_5005_c` did not —
but `check-photos` is the reliable test.

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
python3 -m venv .venv && .venv/bin/pip install anthropic Pillow pillow-heif
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
| **submit** | 07:23 UTC daily (≈2:23am ET) | collect contributor submissions, ingest new photos from Drive, submit a batch, walk away |
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
| `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET` | as in `.env` |
| `SIF_WORKER_URL`, `SIF_PIPELINE_TOKEN` | optional — [contributor mode](#contributor-mode-signing-in-on-the-site). The token is scoped to the `pipeline` role, which may collect the inbox and nothing else. |

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
./scripts/install-watcher.sh                     # read the shared Drive folder
./scripts/install-watcher.sh ~/Dropbox/photos    # ...or a local folder
```

A launchd agent running `autopilot.sh` every 15 minutes. With no argument it reads
the shared Drive folder, the same place the cloud pipeline reads — it used to demand
a local directory, which is how it came to be watching a folder nobody puts photos
in while this README described Drive. Only runs when the machine is awake and
online. Stop it with
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
python3 scripts/plantdb.py occurrences  # species-and-place groupings, not one row per photo
python3 scripts/plantdb.py fieldwork    # what to go and check, and what would settle each
python3 scripts/plantdb.py export-imap  # field-verified invasives, as an iMapInvasives CSV
python3 scripts/plantdb.py inbox-pull   # apply what contributors submitted through the site
python3 scripts/plantdb.py contributor add --name "…" --role verifier
python3 scripts/plantdb.py redundant --file A.jpg --of B.jpg --by "…"  # mark a frame surplus
python3 scripts/plantdb.py withdraw --file A.jpg --by "…" --reason "…"  # take it off the site
python3 scripts/plantdb.py withheld    # everything not carried on the site, and why
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

## Occurrences: one species, one place

An observation is a photograph. An **occurrence** is a thing growing somewhere —
what a land manager treats, what a state database records, and what someone walks
out to check.

```bash
python3 scripts/plantdb.py occurrences        # regulated, invasive and undetermined
python3 scripts/plantdb.py occurrences --all  # everything
```

Photographs of one species within **10 metres** of each other are one occurrence.
That number is empirical, not a guess: in the first real survey batch, photographs
of a single patch sat 0–2.1 m apart and the nearest genuinely separate site was
10.7 m away, so the threshold sits in the gap.

Grouping is **single-link** — a photograph joins if it is within 10 m of *any*
member — so a stand walked along its length chains into one occurrence instead of
splitting at every stride. It is derived from the records, never stored, so a
photograph taken next year at the same spot joins by being where it is. There is no
membership to maintain and nothing to forget to update.

A background sighting counts. A plant caught behind the subject is a real record of
it growing there, and for an invasive it may be the only record there is.

**This is also a correctness fix for the state export.** iMapInvasives records a
species observed at one location on one date, so `export-imap` writes one row per
occurrence. Submitted per photograph, seven shots of one willowherb patch would
report seven infestations to Maine.

### One entry per patch, everywhere

`one_per_patch` is the single rule, and every surface that enumerates records uses
it: the map, the photo grid, the map popup, the species sheet, the steward sheet,
`unverified`, `invasives` and `export-imap`.

A patch photographed seven times is one thing growing in one place. Seven rows in a
reviewer's list is seven walks to verify one shrub; seven lines in a survey report
overstates what is on the ground; seven records to the state reports seven
infestations.

**Every photograph is kept** — they are the evidence, and they all appear on the
species page. Only one of them represents the find in a list, with the others
counted beside it ("7 photographs of this patch"). Grouping is a presentation rule,
never a deletion.

Grouping still *carries* all seven, though — hosted, shipped and shown. Deciding
that six of them are surplus imagery is a separate, human judgement, and it is
[contributor mode](#contributor-mode-signing-in-on-the-site) that records it.

Records with no coordinates each stand alone. Without a location there is no way to
know whether two photographs are the same plant, and merging on a guess would
invent a finding.

## Going back to check: `fieldwork`

```bash
python3 scripts/plantdb.py fieldwork          # regulated and invasive, urgent first
python3 scripts/plantdb.py fieldwork --all    # everything unconfirmed
```

"Go and look at this" is not actionable. What a person needs standing in front of
the plant is the feature that decides it and the thing it might be instead — both
of which the catalogue already holds. `fieldwork` assembles them per occurrence
into something you can carry: coordinates and a maps link, what the photograph
showed, a checklist of features to photograph, the lookalikes to rule out, and the
exact `confirm` command to run afterwards.

Photographs taken there join the occurrence automatically, by location. Ones dated
on or after the day of the check are recorded as the evidence the confirmation
rests on — **recorded, never required.** A steward who went and looked has been
there, phone or no phone, and refusing the verdict would lose the field check
entirely.

A `rejected` verdict removes the occurrence outright rather than leaving it
unconfirmed: a person stood there and said it is not that species, so it should
leave the map and the fieldwork list, not linger as something still to check.

## Getting records to the state — iMapInvasives

Maine tracks invasive species in **iMapInvasives**, run by NatureServe and
coordinated here by the Maine Natural Areas Program. Maine is one of only five
participating jurisdictions, which makes it the place an invasive record has to
land if it is going to count with the state.

```bash
python3 scripts/plantdb.py export-imap
```

**There is no API to submit to.** The documented routes in are manual entry, the
mobile app, and a **bulk upload tool that only a Jurisdiction Administrator can
run**. So this writes the CSV to hand them; nothing is posted anywhere.

Required columns, from NatureServe's published spec: `Source Unique ID`, `Species`,
`Date`, `Observer`, `Latitude`, `Longitude`. Coordinates must be decimal degrees
inside the jurisdiction, and **the Observer must already exist in iMapInvasives** or
the upload fails.

### Only field-verified records are exported, deliberately

An AI identification is a lead. Putting leads into a dataset that land managers act
on is precisely the failure this project exists to avoid, and once they are in they
are not easily taken back.

That constraint also supplies the one required field the survey does not otherwise
collect. iMap wants an **Observer** — the person who observed the species — and for
a field-verified record that is exactly who `verified.by` is. The standard the
project already holds itself to produces the field the state requires.

Native species are skipped too: iMapInvasives tracks invasives, and a confirmed
goldenrod is good survey data but noise to the people receiving the file.

### What this means for the pitch

"A field tool that produces state-submittable records" is a stronger offer than
"another map" — but only once someone has walked out and confirmed something. Until
then `export-imap` correctly writes nothing, and says so.

**One correction worth knowing:** feeding iNaturalist does *not* get data into
iMapInvasives. iNat records reach iMap through a GBIF export as a **view-only
snapshot layer**; they are "not brought directly into the iMapInvasives database"
and "do not undergo the same review & confirmation process". Only hand-picked
high-priority records are keyed in by staff. iNaturalist is worth using for reach
and for FOSI's existing audience — it is not a route to a state record.

## Tests

```bash
python3 -m unittest discover -s tests -t .   # the survey's rules
node --test tests/*.test.mjs                 # the site's rules
python3 tests/mutation_check.py              # do those two still have teeth?
```

No dependencies, no network, no browser download; each is well under a second.
All three run on every push and pull request, and the first two again in the
deploy workflow — the pipeline commits straight to `main` every night, so a broken
rule must not be able to reach the site by skipping a PR.

### Why there are three

The Python suite was 149 tests and green while four user-visible bugs shipped in a
single session: a filter that hid the survey's only invasive, a photograph
credited to one species when it evidenced two, counts that disagreed with the map
beside them, and a button that opened the wrong plant. None were reachable from
Python, because they lived in an inline `<script>` wrapped around a live DOM.

So the page's rules moved into `app/survey.js` — no DOM, no globals, everything
passed in — which the page loads and `tests/app.test.mjs` exercises directly.
Both use that one file, so they cannot drift.

The third suite is the one that matters most. **A green suite proves nothing until
you have watched it fail**, so `mutation_check.py` reintroduces each of those bugs
in turn and requires the suite to go red:

```
  ok   a background-only species is invisible to its own filter
  ok   one frame showing two species counts as one find
  ok   a chip counts species rather than locations
  ok   'Full details' opens the photograph's subject, not the find
  ok   an empty filter paints over the map instead of tearing it down
  ok   waiting for a paint hangs forever in a backgrounded tab
  ok   a backtick in an HTML comment silently breaks the whole page
```

That last one is why the site suite parses the page at all: a stray backtick
inside a template literal terminated the string and broke every script on the
page — no filters, no map, no contributor mode — while the file stayed valid HTML
and all 149 Python tests went on passing.

**What none of them cover is layout.** A CSS collision that lays a checklist out
in columns, or a popup falling off the bottom of a phone, still needs a browser
and a person looking at it.

What they cover is deliberately narrow: **the judgment calls, not the plumbing.**
Each rule below encodes a decision that reads as arbitrary to whoever edits it next,
and would break silently.

| Suite | Holds |
|---|---|
| `test_identification_guard` | Blocks descriptions **without** swallowing "Japanese Knotweed (possible young shoot)". Widen it and a regulated-invasive lead is lost with no error. |
| `test_publishing` | What counts as a survey record; what the site may list as a species |
| `test_reconcile` | Merge, drop and orphan logic — and the three things it must never touch: a seed entry, the target of a field check, the `unknown` sentinel |
| `test_steward_sheet` | What counts as a verification, and refusing a sheet whose columns moved |
| `test_data_integrity` | Reads the committed data: the reference list and catalogue agree, nothing published is missing a location or date, no verification field is incomplete |
| `test_site_assets` | That the published page is complete — a script the page loads but `publish` never copies 404s in the browser and takes the whole app with it, failing nothing else |
| `app.test.mjs` | The rules the page runs on: which species a photograph counts towards, what the numbers mean, and whether the page parses |
| `test_contributor_inbox` | Who may write what — including that the unattended token cannot manufacture a field check, and that `worker/index.js` has not drifted from the table here. What a surplus mark may destroy: never the only record of another species in the frame |

Writing these found a real hole: the hedge-word check ran *after* parentheticals
were stripped, so "Fern (unidentified colony)" with a valid genus in `scientific`
would have passed both tests and entered the catalogue. In production it had only
ever been caught by the rank check, so the two were not the independent tests they
looked like.

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

**The site publishes only species something was actually photographed as.** The
catalogue is two things at once and a reader should only ever see one of them: to
the identifier it is vocabulary, there so the model matches a plant instead of
inventing a name for it; to a reader of a page headed "Sears Island Flora Survey"
it looks like an inventory of the island. Most of that vocabulary has never been
photographed here, and four entries are flagged invasive or regulated — a reviewer
filtering for invasives would have seen them listed beside genuine finds. So
`publish` filters to what has a photograph behind it (`recorded_species`), and the
rest stays in `data/species.json` doing the job it is for.

The same reasoning is why `reconcile` drops auto-created entries nothing references.
Retiring the Orono batch deleted its records but left the species they created —
including *Japanese Knotweed*, status regulated, on a survey of an island it was
never photographed on.

Treat it as a starting vocabulary, not a baseline. Coastal island flora differs
substantially from an inland roadside, and identification quality on this catalogue
does not guarantee quality on Sears Island. **Test with real island photos before
trusting the pipeline** — see the watchlist in `data/invasive-reference.json` for the
species most worth hunting there: knotweed, Oriental bittersweet, black swallow-wort,
glossy buckthorn, and *Phragmites* in the marsh edges.

## License

MIT for the code. Survey records are contributed observations; the reference list is a
working compilation and not an authority.
