"""The gate that stops the catalogue inventing species.

This is the most consequential rule in the project and the easiest to break by
widening. Read the two halves together: everything in `test_blocks_*` must stay
blocked, and everything in `test_allows_*` must stay allowed. Loosening the first
group lets descriptions back into the catalogue, which then get offered to the model
with every future photo. Tightening the second group throws away a regulated
invasive lead, which is the failure this survey exists to prevent.
"""
import unittest

from .context import identify, plantdb, response


class BlocksNonAnswers(unittest.TestCase):
    """Things that are descriptions of a photograph, not names of organisms."""

    def assertBlocked(self, r, because=""):
        why = identify.non_answer(r)
        self.assertIsNotNone(why, f"should have been blocked ({because}): {r!r}")

    def test_blocks_explicit_unidentifiable(self):
        # The model's own word for it, and the primary signal.
        self.assertBlocked(response(identifiable=False, kind="tree",
                                    common="Red Maple", scientific="Acer rubrum"))

    def test_blocks_legacy_other_at_low_confidence(self):
        # The old convention, kept so results cached before `identifiable` existed
        # still resolve the way they did the first time.
        self.assertBlocked({"kind": "other", "confidence": "low",
                            "common": "Something", "scientific": "Acer rubrum"})

    def test_blocks_hedge_word_in_name(self):
        for common in ("Unidentified mature hardwood (bark only)",
                       "Fern (unidentified colony)",
                       "Mixed Moss and Lichen Mat (tree base)",
                       "Trailside Moss (unidentified)",
                       "Unknown sedge",
                       "Assorted grasses"):
            with self.subTest(common=common):
                self.assertBlocked(response(common=common, scientific="Acer rubrum"),
                                   "hedge word in the common name")

    def test_blocks_rank_above_genus(self):
        # A name at family rank or higher names a group, not an organism:
        # "Bryophyta sp." is the mosses, all of them.
        for sci in ("Bryophyta sp.", "Polypodiopsida sp.", "Boletaceae",
                    "Cladoniaceae", "Asterales", "Basidiomycota", "Rosoideae"):
            with self.subTest(scientific=sci):
                self.assertBlocked(response(common="Some Plant", scientific=sci),
                                   "rank above genus")

    def test_blocks_missing_scientific_name(self):
        self.assertBlocked(response(common="Some Plant", scientific=""))
        self.assertBlocked(response(common="Some Plant", scientific="—"))


class AllowsRealIdentifications(unittest.TestCase):
    """Answers a botanist would recognise, including honestly hedged ones."""

    def assertAllowed(self, r, because=""):
        why = identify.non_answer(r)
        self.assertIsNone(why, f"should have been allowed ({because}), got: {why}")

    def test_allows_species_and_genus(self):
        self.assertAllowed(response(common="Red Maple", scientific="Acer rubrum"))
        self.assertAllowed(response(common="Goldenrod", scientific="Solidago sp."),
                           "genus is a fine answer when species is not visible")

    def test_allows_hedged_invasive_lead(self):
        """The one that matters most.

        The survey is explicitly told to flag anything that could be a regulated
        invasive even at low confidence — a false positive costs a walk, a false
        negative misses an infestation while it is still small. So "possible",
        "probable" and "cf." must never read as a non-answer, however much they
        look like hedging.
        """
        self.assertAllowed(response(
            common="Japanese Knotweed (possible young shoot)",
            scientific="Reynoutria japonica (cf.)", confidence="low"))
        self.assertAllowed(response(
            common="Oriental Bittersweet (probable)",
            scientific="Celastrus orbiculatus", confidence="low"))

    def test_allows_honest_two_genus_hedge(self):
        # "one of these two genera" is under-claiming, which the prompt asks for.
        self.assertAllowed(response(common="Small Bracket Fungi (weathered)",
                                    scientific="Trametes / Trichaptum sp."))

    def test_allows_result_cached_before_identifiable_existed(self):
        # Old cache rows have no `identifiable` key; they must not all read as
        # unidentifiable, or a replay would blank every previously good answer.
        self.assertAllowed({"kind": "shrub", "confidence": "medium",
                            "common": "Wild Rose", "scientific": "Rosa sp."})


class CatalogueEntriesAreJudgedTheSameWay(unittest.TestCase):
    """identify.py and reconcile share one definition, so they cannot disagree
    about what a description is — one refuses to create them, the other removes
    the ones that got in before it existed."""

    def test_shared_rule_agrees_on_a_description(self):
        entry = {"common": "Fern (unidentified colony)", "scientific": "Polypodiopsida sp."}
        self.assertIsNotNone(plantdb.non_answer_reason(entry))
        self.assertIsNotNone(identify.non_answer(response(**entry)))

    def test_shared_rule_agrees_on_a_species(self):
        entry = {"common": "Common Milkweed", "scientific": "Asclepias syriaca"}
        self.assertIsNone(plantdb.non_answer_reason(entry))
        self.assertIsNone(identify.non_answer(response(**entry)))


if __name__ == "__main__":
    unittest.main()
