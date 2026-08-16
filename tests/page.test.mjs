/**
 * The page, actually driven — not a description of it.
 *
 * WHY THIS FILE EXISTS, ALONGSIDE app.test.mjs
 *
 * app/survey.js holds the rules, and tests/app.test.mjs exercises them directly.
 * That caught a whole family of bugs. It could not catch this one:
 *
 *   Every find in a merged pin opened the same record. Clicking "Smooth
 *   Bedstraw" in the list opened St John's Wort, and so did clicking Jewelweed,
 *   and so did clicking the honeysuckle.
 *
 * Every rule in survey.js was right, and the page still showed the wrong plant,
 * because the WIRING between the list and the detail was wrong: the popup
 * addressed a row by its photograph, and one photograph is a find of every
 * species it evidences — the frame at 44.4555,-68.8816 is a find of St John's
 * Wort, of jewelweed and of smooth bedstraw at once, so all three rows shared
 * one address.
 *
 * No test over pure functions can see that. It lives in the string of HTML the
 * popup builds and the handler embedded in it. So this file loads index.html's
 * real inline script into a vm, gives it a fake DOM and a Leaflet stub that
 * behaves the way Leaflet actually behaves, and then clicks things: it runs the
 * onclick attribute out of the generated markup and reads what came back.
 *
 * Still no dependencies, no browser, no network — node --test, like next door.
 *
 * The Leaflet stub is deliberately faithful in one respect: invalidateSize() is
 * a no-op until the map has been given a view, exactly as in Leaflet 1.9. That
 * one detail is why a map built in a container the browser had not laid out yet
 * stayed blank forever, and the test at the bottom holds the fix in place.
 */
import { test, describe, before } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const HTML = readFileSync(join(ROOT, "index.html"), "utf8");
const SURVEY = readFileSync(join(ROOT, "app", "survey.js"), "utf8");
// The same extraction app.test.mjs uses: the one inline <script> at the end.
const SCRIPT = HTML.match(/<script>([\s\S]*)<\/script>\s*<\/body>/)[1];

// --- fixture ----------------------------------------------------------------
// The real pin this bug was reported on, reduced to its essentials: one spot,
// two photographs, four finds. The first photograph's subject is St John's Wort
// and it caught jewelweed and smooth bedstraw behind it — the survey's only
// invasive, which has never been the subject of a photograph anywhere.
const LAT = 44.455461, LON = -68.881622;

const species = (id, common, origin_status) =>
  ({ id, common, scientific: `Sci ${id}`, family: "F", kind: "herb",
     summary: "", origin_status, parts: [], id_marks: [], cautions: [],
     lookalikes: [] });

const SPECIES = [
  species("st-johns-wort", "St. John's Wort", "introduced"),
  species("jewelweed", "Jewelweed", "native"),
  species("smooth-bedstraw", "Smooth Bedstraw", "invasive"),
  species("honeysuckle", "Morrow's Honeysuckle", "regulated"),
  species("unknown", "Unidentified", "unknown"),
];

const shot = (file, species_id, extra = {}) =>
  ({ file, species_id, lat: String(LAT), lon: String(LON), confidence: "medium",
     taken: "2026-08-14 13:08:00", identified: "2026-08-14",
     thumb: `thumbs/${file}`, ...extra });

const OBSERVATIONS = [
  shot("a.jpg", "st-johns-wort", { also: ["jewelweed", "smooth-bedstraw"] }),
  shot("b.jpg", "st-johns-wort", { also: ["jewelweed", "smooth-bedstraw"] }),
  shot("c.jpg", "honeysuckle"),
];

/** One patch per species at this spot, as `plantdb.py occurrence_payload` emits. */
const patch = (species_id, files) =>
  ({ species_id, lat: LAT, lon: LON, n: files.length, surplus: 0, files,
     first_seen: "2026-08-14", last_seen: "2026-08-14", spread_m: 0 });

const DB = {
  generated: "2026-08-15",
  species: SPECIES,
  observations: OBSERVATIONS,
  occurrences: [
    patch("st-johns-wort", ["a.jpg", "b.jpg"]),
    patch("jewelweed", ["a.jpg", "b.jpg"]),
    patch("smooth-bedstraw", ["a.jpg", "b.jpg"]),
    patch("honeysuckle", ["c.jpg"]),
  ],
};

// --- a browser, near enough --------------------------------------------------

/** Every element the page asks for exists and answers to everything. */
function fakeNode(id, size) {
  const node = {
    id, innerHTML: "", outerHTML: "", textContent: "", value: "", open: false,
    style: {}, dataset: {}, classList: { add() {}, remove() {}, toggle() {} },
    scrollTop: 0, hidden: false, checked: false, files: [],
    get clientWidth() { return size.w; },
    get clientHeight() { return size.h; },
    getBoundingClientRect: () => ({ width: size.w, height: size.h, top: 0, left: 0 }),
    appendChild() {}, removeChild() {}, remove() {}, setAttribute() {},
    getAttribute: () => null, addEventListener() {}, removeEventListener() {},
    querySelector: () => null, querySelectorAll: () => [], closest: () => null,
    focus() {}, click() {}, showModal() { node.open = true; }, close() { node.open = false; },
    insertAdjacentHTML() {},
  };
  return node;
}

/** Web-mercator pixels, so `drawMarkers`' distance test behaves as in a browser. */
function project(ll, z) {
  const scale = 256 * Math.pow(2, z);
  const s = Math.sin((ll[0] * Math.PI) / 180);
  return {
    x: ((ll[1] + 180) / 360) * scale,
    y: (0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI)) * scale,
    distanceTo(p) { return Math.hypot(this.x - p.x, this.y - p.y); },
  };
}

/**
 * Leaflet, only as much of it as the page touches — and faithful about the one
 * behaviour that has bitten this project: a map that has never been given a view
 * is not "loaded", and invalidateSize() returns early on it without measuring
 * anything. Every recovery path the page has runs through invalidateSize.
 */
function leafletStub(size, resizeObservers) {
  const layerGroup = () => ({
    layers: [],
    addTo() { return this; },
    clearLayers() { this.layers = []; },
    addLayer(l) { this.layers.push(l); return this; },
  });

  const L = {
    map(_id, _opts) {
      return {
        loaded: false, handlers: {}, _size: null, _zoom: 16, removed: false,
        setView(_ll, z) { this.loaded = true; this._zoom = z; this._measure(); return this; },
        _measure() { this._size = { x: size.w, y: size.h }; },
        invalidateSize() { if (this.loaded) this._measure(); return this; },
        getSize() { return this._size || { x: 0, y: 0 }; },
        getZoom() { return this._zoom; },
        getContainer() { return null; },
        project(ll, z) { return project(ll, z); },
        fitBounds(_b, opts) { this._zoom = (opts || {}).maxZoom || this._zoom; return this; },
        on(ev, fn) { (this.handlers[ev] ||= []).push(fn); return this; },
        fire(ev, e) { (this.handlers[ev] || []).forEach(fn => fn(e)); },
        remove() { this.removed = true; },
      };
    },
    tileLayer: () => ({ addTo() { return this; } }),
    layerGroup,
    divIcon: o => o,
    latLngBounds: () => ({ pad() { return this; } }),
    marker(_ll, opts) {
      return {
        opts, popup: null,
        addTo(g) { g.addLayer(this); return this; },
        bindPopup(html) {
          this.popup = {
            html, pinGid: null,
            setContent(h) { this.html = h; },
            update() {},
          };
          return this;
        },
        getPopup() { return this.popup; },
      };
    },
  };
  L.ResizeObserver = null;
  return L;
}

/**
 * Load the real page script and hand back a way to run expressions inside it.
 * `size` is the map container's measured box — start it at zero to reproduce a
 * tab that has not been laid out.
 */
function loadPage({ db = DB, size = { w: 900, h: 600 } } = {}) {
  const nodes = new Map();
  const resizeObservers = [];
  const document = {
    getElementById(id) {
      if (!nodes.has(id)) nodes.set(id, fakeNode(id, size));
      return nodes.get(id);
    },
    createElement: () => fakeNode("", size),
    addEventListener() {},
    activeElement: null,
  };
  document.head = document.getElementById("head");
  document.body = document.getElementById("body");

  const ctx = {
    document,
    PLANT_DB: db,
    L: leafletStub(size, resizeObservers),
    console,
    setTimeout, clearTimeout, setInterval, clearInterval,
    requestAnimationFrame: cb => setTimeout(cb, 0),
    ResizeObserver: class {
      constructor(cb) { this.cb = cb; resizeObservers.push(this); }
      observe() {} disconnect() {}
    },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    location: { hash: "", pathname: "/", search: "" },
    history: { replaceState() {} },
    navigator: { onLine: true },
    matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
    addEventListener() {},
    fetch: () => Promise.reject(new Error("no network in tests")),
    URL, Blob: class {}, FormData: class {}, Image: class {},
  };
  ctx.window = ctx;
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  vm.runInContext(SURVEY, ctx);
  vm.runInContext(SCRIPT, ctx);

  return {
    /** Evaluate an expression in the page's own scope. */
    run: expr => vm.runInContext(expr, ctx),
    /** Let the map's promise chain and timers run. */
    settle: () => new Promise(r => setTimeout(r, 30)),
    /** The container gained a size — tell the page the way a browser would. */
    resizeTo(w, h) { size.w = w; size.h = h; resizeObservers.forEach(o => o.cb()); },
    ctx,
  };
}

/** Click a row: run the onclick attribute the page wrote, and read the result. */
function clickRow(page, html, row) {
  const handlers = [...html.matchAll(/<button class="pop-row" onclick="([^"]*)"/g)]
    .map(m => m[1].replace(/&quot;/g, '"').replace(/&amp;/g, "&"));
  assert.ok(handlers[row], `no row ${row} in the popup`);
  page.run(`openPopup = { html: null, setContent(h) { this.html = h; }, update() {} };`);
  page.run(`(function(event){ ${handlers[row]} })({ stopPropagation() {} })`);
  return page.run("openPopup.html");
}

const nameOf = html => (html.match(/class="pn">([^<]*)/) || [])[1];
const sheetTarget = html => (html.match(/data-id="([^"]*)"/) || [])[1];
const rowNames = html => [...html.matchAll(/class="rn">([^<]*)/g)].map(m => m[1].trim());

// --- the popup ---------------------------------------------------------------

describe("a pin holding several finds", () => {
  let page, list;

  before(async () => {
    page = loadPage();
    await page.settle();
    // The fixture is one spot, so everything merges into a single pin.
    assert.equal(page.run("PIN.length"), 1, "the fixture should draw exactly one pin");
    list = page.run(`popupHTML("0")`);
  });

  test("lists one row per find, not one per photograph", () => {
    // Three photographs, four finds: three species in the first two frames and
    // one in the third. Listing photographs would show three rows and lose two
    // species.
    assert.deepEqual(rowNames(list),
      ["St. John's Wort", "Jewelweed", "Smooth Bedstraw", "Morrow's Honeysuckle"]);
  });

  test("REGRESSION: counts the photographs at this spot once each", () => {
    // The pin used to be handed its finds flattened into loose photographs, so a
    // frame evidencing three species arrived three times and everything counted
    // off that pile was inflated by however many plants were in shot. This spot
    // holds three photographs; the header claimed nine, and a patch resting on a
    // single frame said "3 photographs of this patch".
    assert.match(list, /4 finds at this spot · 3 photographs/);
    assert.match(clickRow(page, list, 2), /2 photographs of this patch/);
  });

  test("REGRESSION: every row opens the find that row names", () => {
    // The bug: rows were addressed by their photograph, and one photograph is a
    // find of every species it evidences, so the first three rows shared an
    // address and all three opened St John's Wort.
    const expected = ["St. John's Wort", "Jewelweed", "Smooth Bedstraw",
                      "Morrow's Honeysuckle"];
    expected.forEach((common, row) => {
      const detail = clickRow(page, list, row);
      assert.equal(nameOf(detail), common,
        `row ${row} says "${common}" and opened "${nameOf(detail)}"`);
    });
  });

  test("REGRESSION: 'Full details' opens the find that was clicked", () => {
    const ids = ["st-johns-wort", "jewelweed", "smooth-bedstraw", "honeysuckle"];
    ids.forEach((id, row) => {
      assert.equal(sheetTarget(clickRow(page, list, row)), id,
        `row ${row} should open the species sheet for ${id}`);
    });
  });

  test("a find opened from the list can get back to the same list", () => {
    // `pinList` used to rebuild the list with no filter at all, so under a
    // filter the way back showed a different set of finds from the one clicked.
    const detail = clickRow(page, list, 2);
    assert.match(detail, /All 4 finds here/);
    page.run(`openPopup = { html: null, setContent(h) { this.html = h; }, update() {} };
              pinList("0");`);
    assert.deepEqual(rowNames(page.run("openPopup.html")), rowNames(list));
  });

  test("a find resting on one photograph does not offer more", () => {
    const detail = clickRow(page, list, 3);
    assert.match(detail, /Morrow's Honeysuckle/);
    assert.doesNotMatch(detail, /photographs of this patch/);
  });
});

describe("a pin reached through a filter", () => {
  // The filter is the reason the popup cannot read the species off the
  // photograph: under "Invasive" this pin is a bedstraw find, and the frame it
  // rests on is a photograph OF St John's Wort.
  let page;

  before(async () => {
    page = loadPage();
    await page.settle();
    // The real filter path, not a global poked from the side: this is what
    // clicking the "Invasive" chip does.
    page.run(`active = new Set(["invasive"]); render();`);
    await page.settle();
  });

  test("opens the species the filter matched, not the photograph's subject", () => {
    assert.equal(page.run("PIN.length"), 1, "the invasive should be on the map");
    const html = page.run(`popupHTML("0")`);
    assert.equal(nameOf(html), "Smooth Bedstraw");
    assert.equal(sheetTarget(html), "smooth-bedstraw");
  });

  test("a filter matching one find leaves nothing to pick from", () => {
    assert.doesNotMatch(page.run(`popupHTML("0")`), /pop-row/,
      "one find should open directly");
  });

  test("the pin is drawn in the colour of the find it is showing", () => {
    // The pin's own species, not its photograph's subject: this frame is a
    // photograph OF St John's Wort, which is merely introduced.
    assert.equal(page.run(`markers.layers.length`), 1);
    assert.match(page.run(`markers.layers[0].opts.icon.html`), /mk s-invasive/);
  });
});

// --- the map's own recovery --------------------------------------------------

describe("a map built before the browser has laid it out", () => {
  test("REGRESSION: it recovers when the container gets a size", async () => {
    // nextFrame's 250ms escape hatch exists so a backgrounded tab cannot hang —
    // which means the chain can arrive before layout and measure zero. Leaflet's
    // invalidateSize() returns early on a map with no view, so every recovery
    // path the page had was a no-op and the map stayed blank with all 65 records
    // still in it. Giving the map a view at creation is what makes it able to
    // re-measure at all.
    const page = loadPage({ size: { w: 0, h: 0 } });
    await page.settle();

    page.resizeTo(900, 600);
    await page.settle();

    assert.ok(page.run("map.getSize().x") > 0,
      "the map never re-measured its container");
    assert.equal(page.run("PIN.length"), 1,
      "the map has a size but drew no pins");
  });
});

// --- layout ------------------------------------------------------------------
// What a vm cannot do is lay anything out, so these read the stylesheet. Both
// are single rules that were changed once and broke the page for everyone.

describe("nothing is laid over anything else", () => {
  const css = HTML.slice(HTML.indexOf("<style>"), HTML.indexOf("</style>"));
  const rule = sel => {
    const m = css.match(new RegExp(`(^|\\n)\\s*${sel}\\s*\\{([^}]*)\\}`));
    return m ? m[2] : null;
  };

  test("REGRESSION: the map cannot paint over the page around it", () => {
    // Leaflet numbers its panes 200-700 and its own .leaflet-container rule is
    // `position:relative` with no z-index — which is not a stacking context. So
    // those panes competed with the whole document and outranked all of it:
    // scrolling slid tiles, markers and an open popup over the header.
    const map = rule("#map");
    assert.ok(map, "#map has no rule any more");
    assert.ok(/isolation\s*:\s*isolate/.test(map) || /z-index\s*:\s*0/.test(map),
      "#map must establish its own stacking context, or Leaflet's panes escape it");
  });

  test("REGRESSION: the header scrolls with the page rather than over it", () => {
    // A sticky header and a viewport-tall map claim the same strip of screen.
    // The page scrolls in one piece instead, which is what was asked for and
    // removes the conflict rather than re-ranking it.
    const header = rule("header");
    assert.ok(header, "the header has no rule any more");
    assert.doesNotMatch(header, /position\s*:\s*(sticky|fixed)/,
      "a pinned header overlaps the map it sits above");
  });
});
