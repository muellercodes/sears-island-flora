"""Grouping photographs into occurrences: one species, one place.

An observation is a photograph. An occurrence is a thing growing somewhere — what
a land manager treats, what a state database records, and what someone walks out
to check. The distinction is not cosmetic: submitted per photograph, seven shots
of one willowherb patch report seven infestations to Maine.

The 10 m radius is empirical. In the first real survey batch, photographs of one
patch sat 0–2.1 m apart and the nearest genuinely separate site was 10.7 m away.
"""
import unittest

from .context import obs, plantdb, species

# ~11 m north, ~8 m east of the anchor at this latitude.
ANCHOR = ("44.455464", "-68.881508")
NEAR = ("44.455500", "-68.881470")      # ~5 m away
FAR = ("44.455700", "-68.881508")       # ~26 m away

KNOTWEED = species("knotweed", "Japanese Knotweed", "Reynoutria japonica",
                   origin_status="regulated")


def at(file, lat, lon, sid="knotweed", **kw):
    o = obs(file, sid, **kw)
    o["lat"], o["lon"] = lat, lon
    return o


class GroupingByPlace(unittest.TestCase):

    def test_photographs_of_one_patch_are_one_occurrence(self):
        recs = [at("a.jpg", *ANCHOR), at("b.jpg", *NEAR)]
        occ = plantdb.occurrences(recs)
        self.assertEqual(len(occ), 1)
        self.assertEqual(len(occ[0]["members"]), 2)

    def test_a_separate_patch_is_a_separate_occurrence(self):
        occ = plantdb.occurrences([at("a.jpg", *ANCHOR), at("b.jpg", *FAR)])
        self.assertEqual(len(occ), 2)

    def test_different_species_at_one_spot_do_not_merge(self):
        occ = plantdb.occurrences([at("a.jpg", *ANCHOR),
                                   at("b.jpg", *ANCHOR, sid="goldenrod")])
        self.assertEqual(len(occ), 2)

    def test_a_straggling_stand_chains_into_one(self):
        """Single-link: each photograph is within 10 m of the last, so a patch
        walked along its length stays one infestation rather than splitting at
        every stride."""
        recs = [at(f"{i}.jpg", f"{44.455464 + i * 0.00007:.6f}", "-68.881508")
                for i in range(5)]                       # ~7.8 m apart in a line
        occ = plantdb.occurrences(recs)
        self.assertEqual(len(occ), 1)
        self.assertEqual(len(occ[0]["members"]), 5)
        self.assertGreater(occ[0]["spread_m"], 25, "and its spread reflects the whole run")

    def test_a_background_sighting_counts(self):
        """A plant caught behind the subject is a real record of it growing there —
        and for an invasive it may be the only record there is."""
        recs = [at("a.jpg", *ANCHOR, sid="goldenrod", also=["knotweed"])]
        occ = plantdb.occurrences(recs)
        self.assertEqual({x["species_id"] for x in occ}, {"goldenrod", "knotweed"})

    def test_unlocated_and_screened_out_records_are_not_placed(self):
        recs = [{**at("a.jpg", *ANCHOR), "lat": "", "lon": ""},
                at("b.jpg", *ANCHOR, rejected=True)]
        self.assertEqual(plantdb.occurrences(recs), [])

    def test_the_radius_is_configurable(self):
        recs = [at("a.jpg", *ANCHOR), at("b.jpg", *FAR)]
        self.assertEqual(len(plantdb.occurrences(recs, radius=50)), 1)
        self.assertEqual(len(plantdb.occurrences(recs, radius=5)), 2)

    def test_urgent_species_sort_first(self):
        recs = [at("a.jpg", *ANCHOR, sid="goldenrod"), at("b.jpg", *FAR)]
        self.assertEqual(plantdb.occurrences(recs)[0]["species_id"], "knotweed")


class ConfirmationAndFollowUpPhotographs(unittest.TestCase):

    CHECK = {"status": "confirmed", "by": "J. Whitten", "date": "2026-08-20"}

    def test_confirming_one_photograph_confirms_the_occurrence(self):
        recs = [at("a.jpg", *ANCHOR, verified=self.CHECK), at("b.jpg", *NEAR)]
        x = plantdb.occurrences(recs)[0]
        self.assertTrue(x["confirmed"])
        self.assertEqual(x["confirmed_by"], "J. Whitten")

    def test_a_later_photograph_joins_by_location_alone(self):
        """The point of deriving this: a photograph taken next year at the same
        spot belongs to the same find, with nothing to remember to update."""
        recs = [at("a.jpg", *ANCHOR, verified=self.CHECK)]
        recs.append({**at("later.jpg", *NEAR), "taken": "2027-06-01 10:00:00 +0000"})
        x = plantdb.occurrences(recs)[0]
        self.assertEqual(len(x["members"]), 2)
        self.assertTrue(x["confirmed"])

    def test_photographs_from_the_check_are_identified(self):
        """Evidence the confirmation rests on: taken on or after the day someone
        stood there."""
        recs = [at("a.jpg", *ANCHOR, verified=self.CHECK),
                {**at("during.jpg", *NEAR), "taken": "2026-08-20 11:00:00 +0000"}]
        x = plantdb.occurrences(recs)[0]
        self.assertEqual([o["file"] for o in x["confirming_photos"]], ["during.jpg"])

    def test_a_confirmation_with_no_photograph_is_still_a_confirmation(self):
        """Recorded, never required. A steward who went and looked has been there,
        phone or no phone — blocking that would lose the field check entirely."""
        x = plantdb.occurrences([at("a.jpg", *ANCHOR, verified=self.CHECK)])[0]
        self.assertTrue(x["confirmed"])
        self.assertEqual(x["confirming_photos"], [])

    def test_a_rejected_verdict_removes_the_occurrence_entirely(self):
        """Stronger than not-confirmed. A person stood there and said it is not
        that species, so there is no longer an occurrence of it at that spot —
        it should vanish from the map and from the fieldwork list, not linger as
        something still awaiting a check."""
        recs = [at("a.jpg", *ANCHOR,
                   verified={"status": "rejected", "by": "J. Whitten"})]
        self.assertEqual(plantdb.occurrences(recs), [])

    def test_a_correction_moves_the_occurrence_to_the_right_species(self):
        recs = [at("a.jpg", *ANCHOR,
                   verified={"status": "corrected", "species_id": "goldenrod",
                             "by": "J. Whitten", "date": "2026-08-20"})]
        x = plantdb.occurrences(recs)
        self.assertEqual([o["species_id"] for o in x], ["goldenrod"])
        self.assertTrue(x[0]["confirmed"], "the human's answer is a confirmed finding")


class WhatReachesTheState(unittest.TestCase):
    """One row per infestation, not per photograph."""

    def test_one_occurrence_is_one_state_record(self):
        check = {"status": "confirmed", "by": "J. Whitten", "date": "2026-08-20"}
        recs = [at("a.jpg", *ANCHOR, verified=check), at("b.jpg", *NEAR),
                at("c.jpg", *NEAR)]
        occ = plantdb.occurrences(recs)
        self.assertEqual(len(occ), 1)
        self.assertEqual(len(occ[0]["members"]), 3,
                         "three photographs, one record for the state")
        self.assertIsNone(plantdb.imap_blocker(occ[0]["members"][0], KNOTWEED))


if __name__ == "__main__":
    unittest.main()


class EverySurfaceListsFindsNotPhotographs(unittest.TestCase):
    """The rule, stated once. A patch photographed seven times is one thing
    growing in one place — seven rows in a steward's sheet is seven walks to
    verify one shrub, and seven lines in a report overstates what is there.

    Every photograph is kept. Only one of them represents the find anywhere that
    enumerates records, which is what `one_per_patch` decides.
    """

    def setUp(self):
        # A patch shot seven times, one separate plant of the same species, and
        # something else entirely at the first spot.
        self.recs = [at(f"burst{i}.jpg", "44.455464", f"-68.88150{i}") for i in range(7)]
        self.recs.append(at("far.jpg", *FAR))
        self.recs.append(at("other.jpg", *ANCHOR, sid="goldenrod"))

    def test_a_burst_collapses_to_one_entry(self):
        finds = plantdb.one_per_patch(self.recs)
        self.assertEqual(len(finds), 3, "7-shot patch + distant plant + other species")
        counts = sorted(n for _, n, _ in finds)
        self.assertEqual(counts, [1, 1, 7])

    def test_the_photographs_are_not_lost(self):
        """Grouping is a presentation rule, never a deletion."""
        finds = plantdb.one_per_patch(self.recs)
        self.assertEqual(sum(n for _, n, _ in finds), len(self.recs))

    def test_one_row_per_photograph_never_reappears(self):
        for radius in (5, 10, 25):
            with self.subTest(radius=radius):
                finds = plantdb.one_per_patch(self.recs, radius=radius)
                self.assertLess(len(finds), len(self.recs))

    def test_a_photograph_counts_once_even_with_background_species(self):
        """Listings are of records, so a shot showing three species is still one
        row — under whatever it is a photograph OF."""
        recs = [at("a.jpg", *ANCHOR, also=["goldenrod", "rubus"])]
        self.assertEqual(len(plantdb.one_per_patch(recs)), 1)

    def test_unlocated_records_each_stand_alone(self):
        """Without coordinates there is no way to know whether two photographs are
        the same plant, and merging on a guess would invent a finding."""
        recs = [{**at(f"n{i}.jpg", *ANCHOR), "lat": "", "lon": ""} for i in range(3)]
        finds = plantdb.one_per_patch(recs)
        self.assertEqual(len(finds), 3)
        self.assertTrue(all(n == 1 for _, n, _ in finds))
