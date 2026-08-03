"""What reaches the public site, and what the site claims was found.

The survey may be read by people with a stake in the answer, so the rule is that
every published record is a photograph taken here, placed, dated, and of something
that could be named — and every species listed has such a photograph behind it.
"""
import unittest

from .context import obs, plantdb, species


class WhatCountsAsASurveyRecord(unittest.TestCase):

    def test_publishes_a_complete_record(self):
        self.assertIsNone(plantdb.withheld_reason(obs("a.jpg", "goldenrod")))

    def test_withholds_a_screened_out_photo(self):
        r = plantdb.withheld_reason(obs("a.jpg", "unknown", rejected=True))
        self.assertIn("screened out", r)

    def test_withholds_when_nothing_could_be_identified(self):
        r = plantdb.withheld_reason(obs("a.jpg", "unknown"))
        self.assertIn("could be identified", r)

    def test_withholds_without_a_location(self):
        """A sighting is a claim that a species was HERE. Without coordinates
        there is nothing to send anyone to check."""
        r = plantdb.withheld_reason({**obs("a.jpg", "goldenrod"), "lat": "", "lon": ""})
        self.assertIn("no location", r)

    def test_withholds_without_a_date(self):
        r = plantdb.withheld_reason({**obs("a.jpg", "goldenrod"), "taken": ""})
        self.assertIn("no capture date", r)

    def test_missing_metadata_outranks_a_field_verification(self):
        """Even a record a person stood in front of is withheld without
        coordinates — there is still nowhere to send the next person."""
        o = {**obs("a.jpg", "goldenrod"), "lat": "", "lon": "",
             "verified": {"status": "confirmed", "by": "J. Whitten"}}
        self.assertIn("no location", plantdb.withheld_reason(o))

    def test_a_background_species_makes_a_record(self):
        """A plant caught behind the subject is still a real record of it
        growing at that spot."""
        self.assertIsNone(plantdb.withheld_reason(obs("a.jpg", "unknown", also=["rubus"])))

    def test_a_field_verdict_makes_a_record(self):
        o = obs("a.jpg", "unknown", verified={"status": "rejected", "by": "J. Whitten"})
        self.assertIsNone(plantdb.withheld_reason(o),
                          "a person went and looked; that is a finding either way")

    def test_is_publishable_agrees_with_withheld_reason(self):
        for o in (obs("a.jpg", "goldenrod"), obs("b.jpg", "unknown"),
                  obs("c.jpg", "goldenrod", rejected=True)):
            self.assertEqual(plantdb.is_publishable(o),
                             plantdb.withheld_reason(o) is None)


class WhatGoesToTheStewards(unittest.TestCase):
    """The sheet is for records a person could still turn into a finding."""

    def test_unidentified_but_located_goes_for_review(self):
        self.assertTrue(plantdb.reviewable(obs("a.jpg", "unknown")),
                        "someone who knows the flora can name it")

    def test_screened_out_does_not(self):
        self.assertFalse(plantdb.reviewable(obs("a.jpg", "unknown", rejected=True)))

    def test_unlocated_does_not(self):
        """There is no column a steward could fill to give it a location, so
        listing it only spends their attention on something that can never publish."""
        self.assertFalse(plantdb.reviewable({**obs("a.jpg", "goldenrod"), "lat": ""}))
        self.assertFalse(plantdb.reviewable({**obs("a.jpg", "goldenrod"), "taken": ""}))


class WhatTheSiteListsAsSpecies(unittest.TestCase):
    """The catalogue is vocabulary for the identifier; the site is an inventory.
    Only the second is published."""

    CATALOGUE = [
        species("goldenrod", "Goldenrod", "Solidago canadensis"),
        species("rubus", "Raspberry / Blackberry", "Rubus sp."),
        species("coltsfoot", "Coltsfoot", "Tussilago farfara", origin_status="invasive"),
        species("unknown", "Unidentified / Habitat shot", "—"),
    ]

    def test_lists_only_species_with_a_photograph(self):
        kept = plantdb.recorded_species(self.CATALOGUE, [obs("a.jpg", "goldenrod")])
        self.assertEqual({s["id"] for s in kept}, {"goldenrod", "unknown"})

    def test_drops_unphotographed_invasives(self):
        """The sharp case: a reviewer filtering for invasives must not be shown a
        species that was never photographed here."""
        kept = plantdb.recorded_species(self.CATALOGUE, [obs("a.jpg", "goldenrod")])
        self.assertNotIn("coltsfoot", {s["id"] for s in kept})

    def test_keeps_a_species_seen_only_in_the_background(self):
        kept = plantdb.recorded_species(
            self.CATALOGUE, [obs("a.jpg", "goldenrod", also=["rubus"])])
        self.assertIn("rubus", {s["id"] for s in kept})

    def test_keeps_a_human_correction_target(self):
        o = obs("a.jpg", "goldenrod",
                verified={"status": "corrected", "species_id": "rubus", "by": "J. W."})
        kept = plantdb.recorded_species(self.CATALOGUE, [o])
        self.assertIn("rubus", {s["id"] for s in kept},
                      "the site groups by what is currently believed")

    def test_always_keeps_the_unknown_fallback(self):
        """The app resolves a missing species through SP.unknown; without it those
        records render as an error instead of a photo."""
        kept = plantdb.recorded_species(self.CATALOGUE, [])
        self.assertIn("unknown", {s["id"] for s in kept})


if __name__ == "__main__":
    unittest.main()
