"""Checks against the repository's own committed data.

Unlike the rest of the suite these read data/, because what they are checking IS
the data: that the reference list and the catalogue still agree, and that the
published set holds to the rule. Read-only — nothing here writes anything.

The regulatory statuses matter beyond tidiness. They drive the badge on every card
and the ordering of the survey report, and someone may act on one.
"""
import json
import pathlib
import unittest

from .context import plantdb

DATA = pathlib.Path(__file__).resolve().parent.parent / "data"
STATUSES = {"native", "introduced", "invasive", "regulated", "unknown"}


def load(name):
    with open(DATA / name) as f:
        return json.load(f)


class ReferenceListAndCatalogueAgree(unittest.TestCase):

    def setUp(self):
        self.ref = load("invasive-reference.json")
        self.species = {s["id"]: s for s in load("species.json")}

    def test_every_classification_names_a_species_that_exists(self):
        unknown = set(self.ref["classification"]) - set(self.species)
        self.assertEqual(unknown, set(),
                         "classification entries for species not in the catalogue "
                         "are dead weight and silently never apply")

    def test_every_status_is_one_the_site_can_render(self):
        for sid, c in self.ref["classification"].items():
            with self.subTest(species=sid):
                self.assertIn(c["status"], STATUSES)
        for w in self.ref["watchlist"]:
            with self.subTest(watchlist=w["scientific"]):
                self.assertIn(w["status"], STATUSES)

    def test_a_regulated_claim_carries_its_reasoning(self):
        """A regulated badge is the strongest claim this survey makes about a
        plant. Each one should say why, so the next person can check it."""
        for sid, c in self.ref["classification"].items():
            if c["status"] == "regulated":
                with self.subTest(species=sid):
                    self.assertTrue(c.get("note", "").strip(),
                                    f"{sid} is flagged regulated with no note")

    def test_the_source_still_says_it_is_not_an_authority(self):
        """However carefully this file is checked, it is a copy — the rule is
        reviewed every 5 years and this is not it. If that caveat ever quietly
        disappears, someone will cite this to an agency.

        Checked as meaning rather than as one literal word, so that rewording the
        note is allowed and dropping the caveat is not.
        """
        source = self.ref["_source"].lower()
        self.assertTrue(any(w in source for w in ("verify", "recheck", "check")),
                        "_source must tell the reader to check it against the rule")
        self.assertIn("not the rule", source,
                      "_source must say plainly that this file is not the authority")


class ThePublishedSetHoldsToTheRule(unittest.TestCase):
    """Belt and braces on the invariant the whole site rests on. `verify` checks
    this too, but only where thumbnails exist — this runs anywhere."""

    def setUp(self):
        self.obs = load("observations.json")

    def test_nothing_publishable_is_missing_a_location_or_date(self):
        for o in self.obs:
            if plantdb.is_publishable(o):
                with self.subTest(file=o["file"]):
                    self.assertTrue(o.get("lat") and o.get("lon"), "no location")
                    self.assertTrue(o.get("taken"), "no capture date")

    def test_no_record_points_at_a_species_that_does_not_exist(self):
        ids = {s["id"] for s in load("species.json")}
        for o in self.obs:
            for sid in [o.get("species_id")] + list(o.get("also") or []):
                with self.subTest(file=o["file"], species=sid):
                    self.assertIn(sid, ids)

    def test_verification_fields_are_only_ever_a_real_field_check(self):
        """Nothing in the pipeline may write these. Any that exist must be
        complete — a status the tool recognises, and somebody's name."""
        for o in self.obs:
            v = o.get("verified")
            if v:
                with self.subTest(file=o["file"]):
                    self.assertIn(v.get("status"), plantdb.VERIFY_STATUS)
                    self.assertTrue(v.get("by", "").strip(),
                                    "an unattributed verification is not a verification")


class TheCatalogueIsClean(unittest.TestCase):

    def test_no_duplicates_and_no_descriptions_are_waiting(self):
        """If this fails, run `plantdb.py reconcile` — it will say what it wants
        to do before it does anything."""
        species, obs = load("species.json"), load("observations.json")
        dropped, merged, _ = plantdb.reconcile(species, obs, apply=False)
        self.assertEqual([d[0]["id"] for d in dropped], [])
        self.assertEqual([k["id"] for k, _, _, _ in merged], [])

    def test_species_ids_are_unique(self):
        ids = [s["id"] for s in load("species.json")]
        self.assertEqual(len(ids), len(set(ids)))


if __name__ == "__main__":
    unittest.main()
