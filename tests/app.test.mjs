/**
 * The site's own tests. `node --test tests/` — no dependencies, no browser, no
 * network, and no build step, in keeping with the Python suite next door.
 *
 * WHY THIS FILE EXISTS
 *
 * Every one of these tests is a bug that reached a human. The Python suite is
 * 149 tests and green, and it caught none of them, because they lived in the
 * browser: a filter that hid the survey's only invasive, a photograph credited
 * to one species when it evidenced two, counts that disagreed with the map
 * beside them, a button that opened the wrong plant, and — worst — a stray
 * backtick that silently broke the entire page.
 *
 * So the rules moved into app/survey.js, which has no DOM, and this exercises
 * them directly. What it cannot check is layout; that still needs a browser. It
 * CAN check every rule about which species a photograph counts towards, what the
 * numbers mean, and whether the page parses at all.
 */
import { test, describe } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
await import(join(ROOT, "app", "survey.js"));
const S = globalThis.Survey;

// --- fixtures ---------------------------------------------------------------
// Coordinates are the real survey's, so distances and grouping behave as they do
// in production rather than in a tidy imaginary space.
const LAT = 44.456192, LON = -68.881839;

const photo = (file, species_id, extra = {}) =>
  ({ file, species_id, lat: String(LAT), lon: String(LON),
     taken: "2026-08-14 13:08:00", ...extra });

/** One occurrence payload entry, as `plantdb.py occurrence_payload` emits it. */
const occ = (species_id, files, extra = {}) =>
  ({ species_id, lat: LAT, lon: LON, n: files.length, surplus: 0,
     files, ...extra });

const setOf = (...ids) => new Set(ids);

describe("which species a photograph counts towards", () => {
  test("a human correction beats the model's answer", () => {
    const o = photo("a.jpg", "willowherb", { effective_species_id: "fireweed" });
    assert.equal(S.spOf(o), "fireweed");
  });

  test("a photograph evidences its subject AND anything behind it", () => {
    const o = photo("a.jpg", "rubus", { also: ["smooth-bedstraw", "dandelion"] });
    assert.deepEqual(S.speciesIn(o).sort(),
                     ["dandelion", "rubus", "smooth-bedstraw"]);
  });

  test("REGRESSION: a background-only species is not invisible to a filter", () => {
    // The survey's one invasive, smooth-bedstraw, has never been the subject of
    // a photograph — it sits behind a rubus cane. Filtering on the subject alone
    // made "Invasive" show nothing at all, hiding the only record there is.
    const o = photo("a.jpg", "rubus", { also: ["smooth-bedstraw"] });
    assert.ok(S.evidences(o, setOf("smooth-bedstraw")));
    assert.equal(S.shownAs(o, setOf("smooth-bedstraw")), "smooth-bedstraw");
  });

  test("with no filter, a photograph is shown as its own subject", () => {
    const o = photo("a.jpg", "rubus", { also: ["smooth-bedstraw"] });
    assert.equal(S.shownAs(o, null), "rubus");
  });

  test("a photograph evidencing nothing in the filter is excluded", () => {
    const o = photo("a.jpg", "rubus", { also: ["dandelion"] });
    assert.equal(S.evidences(o, setOf("japanese-knotweed")), false);
  });

  test("duplicates between subject and background collapse", () => {
    const o = photo("a.jpg", "rubus", { also: ["rubus"] });
    assert.deepEqual(S.speciesIn(o), ["rubus"]);
  });
});

describe("photographs collapsing into finds", () => {
  const index = (...occs) => S.indexOccurrences(occs);

  test("seven shots of one patch are one find", () => {
    const files = ["a.jpg", "b.jpg", "c.jpg", "d.jpg", "e.jpg", "f.jpg", "g.jpg"];
    const units = S.byOccurrence(files.map(f => photo(f, "willowherb")),
                                 null, index(occ("willowherb", files)));
    assert.equal(units.length, 1);
    assert.equal(units[0].items.length, 7);
    assert.equal(units[0].species_id, "willowherb");
  });

  test("REGRESSION: one frame catching two species is two finds, not one", () => {
    // Plantain and dandelion share a frame in this survey. Crediting a
    // photograph only to the first matching species drew 9 introduced locations
    // where the data holds 10 — a whole find silently missing from the map.
    const o = photo("a.jpg", "broadleaf-plantain", { also: ["dandelion"] });
    const units = S.byOccurrence([o], setOf("broadleaf-plantain", "dandelion"),
      index(occ("broadleaf-plantain", ["a.jpg"]), occ("dandelion", ["a.jpg"])));
    assert.equal(units.length, 2);
    assert.deepEqual(units.map(u => u.species_id).sort(),
                     ["broadleaf-plantain", "dandelion"]);
  });

  test("...but only the species the filter asked for", () => {
    const o = photo("a.jpg", "broadleaf-plantain", { also: ["dandelion"] });
    const units = S.byOccurrence([o], setOf("dandelion"),
      index(occ("broadleaf-plantain", ["a.jpg"]), occ("dandelion", ["a.jpg"])));
    assert.equal(units.length, 1);
    assert.equal(units[0].species_id, "dandelion");
  });

  test("the same species in two places is two finds", () => {
    const far = { lat: String(LAT + 0.01), lon: String(LON + 0.01) };
    const units = S.byOccurrence(
      [photo("a.jpg", "goldenrod"), photo("b.jpg", "goldenrod", far)], null,
      index(occ("goldenrod", ["a.jpg"]),
            { ...occ("goldenrod", ["b.jpg"]), lat: LAT + 0.01, lon: LON + 0.01 }));
    assert.equal(units.length, 2);
    assert.equal(S.tally(units).species, 1);
  });

  test("a record with no occurrence stands alone rather than vanishing", () => {
    // Records with no coordinates are in no occurrence. They must still appear
    // as their own unit, or a photograph disappears from the survey entirely.
    const o = photo("lonely.jpg", "goldenrod", { lat: "", lon: "" });
    const units = S.byOccurrence([o], null, index());
    assert.equal(units.length, 1);
    assert.equal(units[0].occ, null);
  });

  test("two unlocated records do not merge into one another", () => {
    const a = photo("a.jpg", "goldenrod", { lat: "", lon: "" });
    const b = photo("b.jpg", "goldenrod", { lat: "", lon: "" });
    assert.equal(S.byOccurrence([a, b], null, index()).length, 2);
  });

  test("an empty set of records yields no finds rather than throwing", () => {
    assert.deepEqual(S.byOccurrence([], null, index()), []);
    assert.deepEqual(S.byOccurrence(null, null, index()), []);
  });
});

describe("what the numbers on screen mean", () => {
  const index = (...occs) => S.indexOccurrences(occs);

  test("species, locations and photographs are three different counts", () => {
    // 1 species, 2 places, 3 photographs — the shape that made "Native 20" over
    // a map of 32 look like a bug when both numbers were right.
    const far = { lat: String(LAT + 0.01), lon: String(LON + 0.01) };
    const units = S.byOccurrence(
      [photo("a.jpg", "goldenrod"), photo("b.jpg", "goldenrod"),
       photo("c.jpg", "goldenrod", far)], null,
      index(occ("goldenrod", ["a.jpg", "b.jpg"]),
            { ...occ("goldenrod", ["c.jpg"]), lat: LAT + 0.01, lon: LON + 0.01 }));
    assert.deepEqual(S.tally(units), { species: 1, locations: 2, photographs: 3 });
  });

  test("REGRESSION: a photograph is counted once, however many plants are in it", () => {
    // Once a frame produced a find per species it evidences — which it must, or
    // a background-only species has no location — summing items across units
    // counted one photograph once per plant in it. The page said 96 photographs
    // over a survey of 68, while the header beside it said 68.
    const o = photo("a.jpg", "broadleaf-plantain", { also: ["dandelion", "rubus"] });
    const units = S.byOccurrence([o], null,
      index(occ("broadleaf-plantain", ["a.jpg"]), occ("dandelion", ["a.jpg"]),
            occ("rubus", ["a.jpg"])));
    assert.equal(units.length, 3, "three species means three finds");
    assert.equal(S.tally(units).photographs, 1, "but only one photograph");
  });

  test("the same photograph in two finds of one species counts once", () => {
    const far = { lat: String(LAT + 0.01), lon: String(LON + 0.01) };
    const a = photo("a.jpg", "goldenrod");
    const b = photo("b.jpg", "goldenrod", far);
    const units = S.byOccurrence([a, b], null,
      index(occ("goldenrod", ["a.jpg"]),
            { ...occ("goldenrod", ["b.jpg"]), lat: LAT + 0.01, lon: LON + 0.01 }));
    assert.equal(S.tally(units).photographs, 2);
  });

  test("REGRESSION: a chip counts locations, so it can equal what the map draws", () => {
    // Species can never agree with the map: one species in four places is four
    // pins. The chip counts places, which is the only thing that can match.
    const status = id => ({ goldenrod: "native", "smooth-bedstraw": "invasive" }[id]);
    const counts = S.locationCounts(
      [occ("goldenrod", ["a.jpg"]), occ("goldenrod", ["b.jpg"]),
       occ("goldenrod", ["c.jpg"]), occ("goldenrod", ["d.jpg"]),
       occ("smooth-bedstraw", ["e.jpg"])], status);
    assert.deepEqual(counts, { native: 4, invasive: 1 });
  });

  test("the `unknown` sentinel is never counted as a species", () => {
    const counts = S.locationCounts(
      [occ("unknown", ["a.jpg"]), occ("goldenrod", ["b.jpg"])], () => "native");
    assert.deepEqual(counts, { native: 1 });
  });

  test("a species missing from the catalogue is skipped, not crashed on", () => {
    const counts = S.locationCounts([occ("ghost-species", ["a.jpg"])], () => null);
    assert.deepEqual(counts, {});
  });
});

describe("the published page itself", () => {
  const html = readFileSync(join(ROOT, "index.html"), "utf8");
  const script = html.match(/<script>([\s\S]*)<\/script>\s*<\/body>/)[1];

  test("REGRESSION: the inline script parses", () => {
    // A backtick inside an HTML comment inside a template literal terminated the
    // string and broke the whole page — no filters, no map, no contributor mode.
    // Nothing else in either suite would have noticed: the file is still valid
    // HTML and every Python test still passed.
    assert.doesNotThrow(() => new Function(script));
  });

  test("no HTML comment sits inside the script carrying a backtick", () => {
    const comments = script.match(/<!--[\s\S]*?-->/g) || [];
    const risky = comments.filter(c => c.includes("`"));
    assert.deepEqual(risky, [], "an HTML comment in <script> contains a backtick");
  });

  test("the page loads the shared rules rather than copying them", () => {
    assert.match(html, /<script src="app\/survey\.js"><\/script>/);
    for (const fn of ["byOccurrence", "shownAs", "tally", "locationCounts"]) {
      assert.ok(script.includes(`Survey.${fn}`),
                `index.html should call Survey.${fn}, not reimplement it`);
    }
  });

  test("REGRESSION: every click that opens a species sheet uses the shown species", () => {
    // "Full details" read the photograph's raw subject while the popup around it
    // named the matched species, so the bedstraw pin opened St John's Wort.
    const targets = script.match(/data-id="\$\{[^}]*\}"/g) || [];
    const raw = targets.filter(t => /o\.species_id|\.species_id\b/.test(t)
                                 && !/s\.id|u\.species_id/.test(t));
    assert.deepEqual(raw, [],
      "a click target reads the photograph's subject instead of the shown species");
  });

  test("the map is torn down, not painted over, when a filter matches nothing", () => {
    // Overwriting #map's innerHTML while Leaflet still held the element left the
    // two out of step and every later filter drew into a container Leaflet no
    // longer had — one empty category killed the map until you switched views.
    assert.match(script, /map\.remove\(\);\s*map = markers = null;/);
  });

  test("waiting for a paint cannot hang forever", () => {
    // requestAnimationFrame does not fire in a backgrounded tab, so a map first
    // rendered while hidden waited on a frame that never came.
    const nf = script.match(/const nextFrame[\s\S]{0,400}/);
    assert.ok(nf, "nextFrame has gone missing");
    assert.match(nf[0], /requestAnimationFrame/);
    assert.match(nf[0], /setTimeout/,
                 "nextFrame needs a timeout fallback for backgrounded tabs");
  });
});

describe("against the real published survey", () => {
  // The fixtures above prove the rules; this proves they hold on the actual
  // data, which is where the surprises have been. Skips cleanly on a fresh
  // clone, where public/ has not been built yet.
  let DB = null;
  try {
    const js = readFileSync(join(ROOT, "public", "app", "data.js"), "utf8");
    DB = JSON.parse(js.replace(/^window\.PLANT_DB = /, "").replace(/;\s*$/, ""));
  } catch { /* not built — the tests below skip */ }

  const guard = (name, fn) => test(name, { skip: !DB && "public/ not built" }, fn);

  guard("every chip total equals the finds the map would draw for it", () => {
    const SP = Object.fromEntries(DB.species.map(s => [s.id, s]));
    const statusOf = id => (SP[id] ? (SP[id].origin_status || "unknown") : null);
    const chips = S.locationCounts(DB.occurrences, statusOf);
    const index = S.indexOccurrences(DB.occurrences);

    for (const [status, chipCount] of Object.entries(chips)) {
      const ok = new Set(DB.species.filter(s => s.id !== "unknown"
        && (s.origin_status || "unknown") === status).map(s => s.id));
      const shots = DB.observations.filter(o => S.evidences(o, ok));
      const located = S.byOccurrence(shots, ok, index)
        .filter(u => u.occ !== null);
      assert.equal(located.length, chipCount,
        `chip says ${chipCount} ${status} locations, map would draw ${located.length}`);
    }
  });

  guard("every species with a photograph is reachable by its own filter", () => {
    // The bug that hid smooth-bedstraw, generalised: no species that something
    // was photographed as may be unreachable through the interface.
    const index = S.indexOccurrences(DB.occurrences);
    const withPhotos = new Set();
    DB.observations.forEach(o => S.speciesIn(o).forEach(s => withPhotos.add(s)));
    for (const sid of withPhotos) {
      if (sid === "unknown") continue;
      const ok = new Set([sid]);
      const units = S.byOccurrence(DB.observations.filter(o => S.evidences(o, ok)),
                                   ok, index);
      assert.ok(units.length > 0, `${sid} has photographs but no reachable find`);
      assert.ok(units.every(u => u.species_id === sid),
                `${sid} filter produced a find labelled as something else`);
    }
  });

  guard("no published record is withdrawn, surplus, or missing a location", () => {
    for (const o of DB.observations) {
      assert.equal(o.withdrawn, undefined, `${o.file} is withdrawn but published`);
      assert.equal(o.redundant, undefined, `${o.file} is surplus but published`);
      assert.ok(o.lat && o.lon, `${o.file} is published with no location`);
      assert.ok(o.taken, `${o.file} is published with no capture date`);
    }
  });

  guard("the photograph count never exceeds the survey's photographs", () => {
    // The whole-survey view must agree with the data it is built from. Nothing
    // on the page may claim more photographs than exist.
    const index = S.indexOccurrences(DB.occurrences);
    const units = S.byOccurrence(DB.observations, null, index);
    assert.equal(S.tally(units).photographs, DB.observations.length);
  });

  guard("photograph counts on finds match the records behind them", () => {
    const files = new Set(DB.observations.map(o => o.file));
    for (const x of DB.occurrences) {
      const present = x.files.filter(f => files.has(f));
      assert.equal(present.length, x.n,
        `${x.species_id} claims ${x.n} photographs, ${present.length} are published`);
    }
  });
});
