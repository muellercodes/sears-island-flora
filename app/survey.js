/**
 * The survey's presentation rules, with no DOM and no globals.
 *
 * WHY THIS IS A SEPARATE FILE
 *
 * These functions decide which species a photograph counts towards, how
 * photographs collapse into finds, and what the numbers on screen mean. Every
 * user-visible bug this project has shipped has been in exactly this logic — a
 * filter that hid the only record of an invasive, a photograph credited to one
 * species when it evidenced two, a count that disagreed with the map beside it,
 * a button that opened the wrong plant.
 *
 * None of them were reachable by the Python suite, because they lived inside an
 * inline <script> wrapped around a live DOM. Pulled out here they are ordinary
 * functions over ordinary data, and tests/app.test.mjs runs them directly.
 *
 * Nothing in here may touch `document`, `window`, `L`, or module-level mutable
 * state. Everything it needs is an argument. That is what makes it testable, and
 * it is also what stops the page and the tests drifting apart: both load THIS
 * file, so a rule can only be changed in one place.
 *
 * Loaded as a plain <script> by index.html and imported by the tests; it assigns
 * to globalThis so both see the same object.
 */
const Survey = {
  /** The species to believe: a human correction where there is one. */
  spOf(o) {
    return o.effective_species_id || o.species_id;
  },

  /** Every species a photograph is evidence of — its subject and anything
   *  identified behind it. A plant caught in the background is a real record of
   *  it growing there, and for an invasive it may be the only one there is. */
  speciesIn(o) {
    return [...new Set([Survey.spOf(o), ...(o.also || [])])];
  },

  /** Does this photograph evidence anything in `ok`? */
  evidences(o, ok) {
    return !ok || Survey.speciesIn(o).some((s) => ok.has(s));
  },

  /**
   * Which species a photograph is being SHOWN FOR. Usually its subject, but
   * under a filter it is whichever matching species the frame evidences: a photo
   * of rubus that also caught smooth-bedstraw is, when filtering to invasives, a
   * record of the bedstraw. Labelling it rubus hid the only record of the
   * survey's one invasive species.
   */
  shownAs(o, ok) {
    if (!ok) return Survey.spOf(o);
    return Survey.speciesIn(o).find((s) => ok.has(s)) || Survey.spOf(o);
  },

  /**
   * Index occurrences by "<species>|<file>", so a photograph can be looked up
   * under each species it evidences — it belongs to a different patch of each.
   */
  indexOccurrences(occurrences) {
    const by = {};
    (occurrences || []).forEach((x) =>
      (x.files || []).forEach((f) => {
        by[`${x.species_id}|${f}`] = x;
      }));
    return by;
  },

  /**
   * Collapse photographs into the things they are photographs OF. Seven shots of
   * one willowherb patch are one find.
   *
   * A photograph produces one unit per MATCHING species, not one unit total.
   * Crediting only the first lost a find whenever one frame caught two species
   * of the same status — plantain and dandelion share a frame here, and the map
   * drew 9 introduced locations where the data holds 10.
   */
  byOccurrence(records, ok, occIndex) {
    const seen = new Map();
    (records || []).forEach((o) => {
      const sids = Survey.speciesIn(o).filter((s) => !ok || ok.has(s));
      (sids.length ? sids : [Survey.spOf(o)]).forEach((sid) => {
        const x = occIndex[`${sid}|${o.file}`] || null;
        const k = x ? `${sid}@${x.lat},${x.lon}` : `solo|${sid}|${o.file}`;
        if (seen.has(k)) seen.get(k).items.push(o);
        else seen.set(k, { anchor: o, items: [o], occ: x, species_id: sid });
      });
    });
    return [...seen.values()];
  },

  /**
   * What the line under the filters says: species, locations, photographs.
   * Three different things, which is why they are named rather than left as bare
   * numbers for a reader to try to reconcile.
   *
   * `photographs` counts DISTINCT files. Once a photograph began producing a
   * find for every species it evidences — which it must, or a background-only
   * species has no location — summing `items` across units counted one frame
   * once per plant in it. The page claimed 96 photographs over a survey holding
   * 68, and the header beside it said 68, so the site contradicted itself about
   * the one number a reader can check by eye.
   */
  tally(units) {
    const files = new Set();
    units.forEach((u) => u.items.forEach((o) => files.add(o.file)));
    return {
      species: new Set(units.map((u) => u.species_id)).size,
      locations: units.length,
      photographs: files.size,
    };
  },

  /**
   * How many LOCATIONS each status has — the number on a chip.
   *
   * Locations, not species, because a chip's number has to equal what the map
   * draws or it reads as a bug: one species growing in four places is four pins,
   * and goldenrod does exactly that. Species can never be made to agree.
   */
  locationCounts(occurrences, statusOfId) {
    const out = {};
    (occurrences || []).forEach((x) => {
      if (x.species_id === "unknown") return;
      const k = statusOfId(x.species_id);
      if (!k) return;
      out[k] = (out[k] || 0) + 1;
    });
    return out;
  },
};

globalThis.Survey = Survey;
