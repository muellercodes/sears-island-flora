#!/usr/bin/env python3
"""Reintroduce every bug that has reached a person, and check the suite goes red.

    python3 tests/mutation_check.py

A green test suite proves nothing until you have watched it fail. This project
shipped four user-visible bugs in one session while 149 Python tests stayed
green, so "we have tests" is not the reassurance it sounds like — the question is
always whether they would notice.

Each entry below is a real bug, written as the smallest edit that brings it back.
The check passes only if every one of them turns the suite red.

Deliberately NOT named test_*.py: it edits tracked files, and `unittest discover`
picking it up would mean an ordinary test run mutating the working tree. It
restores every file in a finally block, including on Ctrl-C.
"""
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
# Discovered, not listed: a suite added later has to be run by this too, and the
# way that gets forgotten is a hard-coded filename.
SUITES = [str(p.relative_to(ROOT)) for p in sorted((ROOT / "tests").glob("*.test.mjs"))]

# (file, what the bug was, the correct code, the broken code)
MUTATIONS = [
    ("app/survey.js",
     # Was `shownAs` returning the subject; that function has been retired and
     # the rule now lives on the find itself, so the bug is reintroduced there.
     "a find is labelled with its photograph's subject, not what it is a find of",
     "seen.set(k, { anchor: o, items: [o], occ: x, species_id: sid });",
     "seen.set(k, { anchor: o, items: [o], occ: x, species_id: Survey.spOf(o) });"),

    ("app/survey.js",
     "a filter matches only a photograph's subject",
     "return !ok || Survey.speciesIn(o).some((s) => ok.has(s));",
     "return !ok || ok.has(Survey.spOf(o));"),

    ("app/survey.js",
     "one frame showing two species counts as one find",
     "(sids.length ? sids : [Survey.spOf(o)]).forEach((sid) => {",
     "(sids.length ? [sids[0]] : [Survey.spOf(o)]).forEach((sid) => {"),

    ("app/survey.js",
     "a chip counts species rather than locations",
     "out[k] = (out[k] || 0) + 1;",
     "out[k] = 1;"),

    ("app/survey.js",
     "a photograph is counted once per plant in it",
     "units.forEach((u) => u.items.forEach((o) => files.add(o.file)));",
     "units.forEach((u) => u.items.forEach((o, i) => files.add(o.file + i + Math.random())));"),

    ("index.html",
     "'Full details' opens the photograph's subject, not the find",
     '<button data-id="${esc(s.id)}" onclick="openSheet(this.dataset.id)">Full details</button>',
     '<button data-id="${esc(o.species_id)}" onclick="openSheet(this.dataset.id)">Full details</button>'),

    ("index.html",
     "an empty filter paints over the map instead of tearing it down",
     "if (map) { map.remove(); map = markers = null; }",
     "if (map) { /* leave it */ }"),

    ("index.html",
     "waiting for a paint hangs forever in a backgrounded tab",
     "  setTimeout(go, 250);\n",
     "\n"),

    ("index.html",
     "a backtick in an HTML comment silently breaks the whole page",
     "<script>\nconst DB = window.PLANT_DB",
     "<script>\n// <!-- a `backtick` in a comment -->\nconst DB = window.PLANT_DB"),

    ("index.html",
     "every find in a merged pin opens the same record",
     # The original: rows addressed by their photograph, so every find resting on
     # one frame — three species in one shot — shared the first one's address.
     "onclick=\"event.stopPropagation();pinShow('${gid}',${i})\"",
     "onclick=\"event.stopPropagation();pinShow('${gid}',"
     "${units.findIndex(x => x.anchor === u.anchor)})\""),

    ("index.html",
     "a popup names the photograph's subject rather than the find",
     "const o = u.anchor, s = SP[u.species_id] || SP.unknown, n = u.items.length;\n  const finds",
     "const o = u.anchor, s = SP[spOf(u.anchor)] || SP.unknown, n = u.items.length;\n  const finds"),

    ("index.html",
     "a pin flattens its finds back into loose photographs",
     "    if (g) g.units.push(u);\n    else groups.push({ p, units: [u] });",
     "    if (g) g.units.push(...u.items.map(x => ({ ...u, items: [x] })));\n"
     "    else groups.push({ p, units: u.items.map(x => ({ ...u, items: [x] })) });"),

    ("index.html",
     "a popup counts one photograph once per plant in it",
     "photos = Survey.tally(units).photographs",
     "photos = units.reduce((t, u) => t + u.items.length, 0)"),

    ("index.html",
     "a map built before layout can never re-measure its container",
     "      map.setView(llOf(located[0].anchor), 16);\n",
     ""),

    ("index.html",
     "the map paints over the page instead of staying inside its own box",
     "position:relative; isolation:isolate; z-index:0; }",
     "}"),

    ("index.html",
     "a pinned header and a full-height map claim the same screen",
     "  header { background:var(--bg);",
     "  header { position:sticky; top:0; z-index:20; background:var(--bg);"),
]


def main():
    originals = {}
    missed, unapplied = [], []
    try:
        for rel, name, good, bad in MUTATIONS:
            path = ROOT / rel
            text = originals.setdefault(rel, path.read_text())
            if good not in text:
                # The code moved and this mutation no longer describes it. Louder
                # than a silent skip: an unapplied mutation is an untested bug.
                print(f"  ??   {name}\n         anchor no longer in {rel}")
                unapplied.append(name)
                continue
            path.write_text(text.replace(good, bad, 1))
            try:
                # Every site suite, not one file: page.test.mjs drives the page
                # itself and is the only thing that sees a wrong record open.
                r = subprocess.run(["node", "--test", *sorted(SUITES)],
                                   cwd=ROOT, capture_output=True, text=True)
                caught = r.returncode != 0
            finally:
                path.write_text(text)
            print(f"  {'ok  ' if caught else 'MISS'} {name}")
            if not caught:
                missed.append(name)
    finally:
        for rel, text in originals.items():
            (ROOT / rel).write_text(text)

    print()
    if missed or unapplied:
        for n in missed:
            print(f"  ! nothing failed when: {n}")
        for n in unapplied:
            print(f"  ! could not reintroduce: {n}")
        print("\nThe suite would let one of these through again.")
        return 1
    print(f"All {len(MUTATIONS)} known bugs are caught by the suite.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
