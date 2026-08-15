"""The published site has to be complete, and nothing else notices when it isn't.

A missing script does not fail a build, fail a test, or look wrong in the repo.
It 404s in someone's browser and takes the whole application with it — no
filters, no map, no contributor mode — on the one surface this project exists to
produce. That happened the moment app/survey.js was split out of index.html:
`publish` copied index.html and wrote app/data.js, and shipped a page referring
to a file that was not there.
"""
import pathlib
import re
import unittest

from .context import plantdb

ROOT = pathlib.Path(__file__).resolve().parent.parent
INDEX = ROOT / "index.html"


def referenced_scripts():
    return re.findall(r'<script src="app/([A-Za-z0-9_.-]+)"', INDEX.read_text())


class ThePageAndItsAssetsAgree(unittest.TestCase):
    def test_every_script_the_page_loads_exists(self):
        """Except data.js, which `build` and `publish` generate."""
        for name in referenced_scripts():
            if name == "data.js":
                continue
            self.assertTrue((ROOT / "app" / name).exists(),
                            f"index.html loads app/{name}, which is not in app/")

    def test_publish_copies_them_rather_than_a_hard_coded_list(self):
        """Scanned from the page, so adding a script cannot be forgotten here.

        A hard-coded list is the version of this that breaks: it stays correct
        until somebody adds a file, which is exactly when it stops being correct
        and nothing says so.
        """
        src = (ROOT / "scripts" / "plantdb.py").read_text()
        publish = src[src.index("def cmd_publish"):]
        publish = publish[:publish.index("\ndef ")]
        self.assertIn('<script src="app/', publish,
                      "publish should discover assets by scanning index.html")

    def test_the_shared_rules_are_loaded_not_duplicated(self):
        """app/survey.js holds the rules the tests exercise. If the page stopped
        loading it and reimplemented them inline, tests/app.test.mjs would go on
        passing while testing code nobody runs."""
        self.assertIn("survey.js", referenced_scripts())

    def test_generated_data_is_not_committed(self):
        """app/data.js merges in local-only records, so it is gitignored. A
        sibling in the same directory must not accidentally be swept in with it."""
        ignored = (ROOT / ".gitignore").read_text()
        self.assertIn("app/data.js", ignored)
        self.assertNotIn("app/*.js", ignored)
        self.assertNotIn("app/", ignored.split("app/data.js")[0].splitlines()[-1:] or [""])


class TheSiteNeverRecomputesGeography(unittest.TestCase):
    """Grouping photographs into finds happens once, in Python, and the answer
    ships in the occurrence payload.

    If the site ever did its own distance arithmetic it would need its own copy
    of OCCURRENCE_RADIUS_M — a number chosen from the data, where a real patch
    spans 0–2.1 m and the nearest separate site is 10.7 m away — and the
    checklist a volunteer reads could then describe a different find from the
    record the state receives. So the rule is not that the constant is absent
    from the text; it is that the JS does no geography at all.
    """

    def test_survey_js_does_no_distance_arithmetic(self):
        js = (ROOT / "app" / "survey.js").read_text()
        for tell in ("Math.hypot", "Math.cos", "111_320", "111320", "Math.sqrt"):
            self.assertNotIn(tell, js,
                             f"app/survey.js computes distances ({tell}); grouping "
                             "must come from the published occurrences")

    def test_the_radius_lives_in_exactly_one_place(self):
        py = (ROOT / "scripts" / "plantdb.py").read_text()
        self.assertEqual(py.count("OCCURRENCE_RADIUS_M ="), 1)
        self.assertIsInstance(plantdb.OCCURRENCE_RADIUS_M, int)


if __name__ == "__main__":
    unittest.main()
