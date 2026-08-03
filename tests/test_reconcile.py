"""Repairing a catalogue written by a machine.

Three failures, each fixed after the fact because none can be prevented at the
moment of writing, and three things reconcile must never touch.
"""
import unittest

from .context import auto, obs, plantdb, species


class MergesNearDuplicates(unittest.TestCase):
    """Every request in a batch is built from the catalogue as it stood at
    submission, so no request can see an entry a sibling created."""

    def setUp(self):
        self.sp = [
            auto("pixie-cup-lichen", "Pixie-cup Lichen", "Cladonia sp.", id_marks=["a"]),
            auto("pixie-cup-trumpet", "Pixie Cup Lichen (trumpet lichen)", "Cladonia sp.",
                 id_marks=["a", "b", "c"]),
        ]
        self.obs = [obs("1.jpg", "pixie-cup-lichen"), obs("2.jpg", "pixie-cup-trumpet")]

    def test_merges_on_the_same_normalised_common_name(self):
        _, merged, renames = plantdb.reconcile(self.sp, self.obs, apply=True)
        self.assertEqual(len(merged), 1)
        self.assertEqual([s["id"] for s in self.sp], ["pixie-cup-lichen"])
        self.assertEqual(renames, {"pixie-cup-trumpet": "pixie-cup-lichen"})

    def test_keeps_the_older_id_and_the_fuller_write_up(self):
        """The id may already be a link on the published site; the write-up is
        what actually serves a reader."""
        plantdb.reconcile(self.sp, self.obs, apply=True)
        self.assertEqual(self.sp[0]["id"], "pixie-cup-lichen")
        self.assertEqual(self.sp[0]["id_marks"], ["a", "b", "c"])
        self.assertEqual(self.sp[0]["merged_from"], ["pixie-cup-trumpet"])

    def test_repoints_every_reference(self):
        o = [obs("1.jpg", "pixie-cup-trumpet"),
             obs("2.jpg", "goldenrod", also=["pixie-cup-trumpet"]),
             obs("3.jpg", "goldenrod",
                 verified={"status": "corrected", "species_id": "pixie-cup-trumpet",
                           "by": "J. Whitten", "notes": "hers"})]
        plantdb.reconcile(self.sp, o, apply=True)
        self.assertEqual(o[0]["species_id"], "pixie-cup-lichen")
        self.assertEqual(o[1]["also"], ["pixie-cup-lichen"])
        self.assertEqual(o[2]["verified"]["species_id"], "pixie-cup-lichen",
                         "a merge renames a species; it does not overrule a verdict")
        self.assertEqual(o[2]["verified"]["notes"], "hers",
                         "nothing else under `verified` may be touched")

    def test_never_merges_two_species_sharing_a_genus(self):
        sp = [auto("red-clover", "Red Clover", "Trifolium pratense"),
              auto("white-clover", "White Clover", "Trifolium repens")]
        _, merged, _ = plantdb.reconcile(sp, [obs("1.jpg", "red-clover"),
                                              obs("2.jpg", "white-clover")], apply=True)
        self.assertEqual(merged, [], "two species in one genus are two species")
        self.assertEqual(len(sp), 2)


class DropsDescriptions(unittest.TestCase):

    def test_drops_a_description_and_frees_its_records(self):
        sp = [auto("fern-colony", "Fern (unidentified colony)", "Polypodiopsida sp.")]
        o = [obs("1.jpg", "fern-colony", note="Dense colony, no fertile fronds.")]
        dropped, _, _ = plantdb.reconcile(sp, o, apply=True)

        self.assertEqual([d[0]["id"] for d in dropped], ["fern-colony"])
        self.assertEqual(sp, [])
        self.assertEqual(o[0]["species_id"], "unknown")
        self.assertIn("Dense colony", o[0]["note"], "the model's own note survives")
        self.assertIn("Catalogue entry", o[0]["note"], "and says what was removed")

    def test_does_not_requeue_the_photo_for_the_next_paid_run(self):
        """`unidentified` would put it back in the queue for the next ordinary run,
        which would buy the same non-answer again."""
        sp = [auto("fern-colony", "Fern (unidentified colony)", "Polypodiopsida sp.")]
        o = [obs("1.jpg", "fern-colony", confidence="medium")]
        plantdb.reconcile(sp, o, apply=True)
        self.assertEqual(o[0]["confidence"], "low")


class DropsOrphans(unittest.TestCase):
    """An auto entry exists because a photo matched it. When the records go —
    `remove --batch`, a re-identification — the entry is left with nothing behind
    it. That is how retiring the Orono batch left a regulated knotweed entry on a
    survey of an island it was never photographed on."""

    def test_drops_an_entry_nothing_references(self):
        sp = [auto("knotweed", "Japanese Knotweed", "Reynoutria japonica",
                   origin_status="regulated")]
        dropped, _, _ = plantdb.reconcile(sp, [], apply=True)
        self.assertEqual([d[0]["id"] for d in dropped], ["knotweed"])
        self.assertEqual(sp, [])

    def test_keeps_an_entry_a_photo_still_uses(self):
        sp = [auto("goldenrod", "Goldenrod", "Solidago canadensis")]
        dropped, _, _ = plantdb.reconcile(sp, [obs("1.jpg", "goldenrod")], apply=True)
        self.assertEqual(dropped, [])

    def test_keeps_an_entry_only_seen_in_the_background(self):
        sp = [auto("rubus", "Raspberry / Blackberry", "Rubus sp.")]
        dropped, _, _ = plantdb.reconcile(
            sp, [obs("1.jpg", "goldenrod", also=["rubus"])], apply=True)
        self.assertEqual(dropped, [])


class RefusesToTouch(unittest.TestCase):
    """Three protections. Each one is an unattended run declining to destroy
    something a person meant."""

    def test_never_drops_a_seed_entry(self):
        """The seed catalogue holds hedged entries a person put there on purpose —
        "Bolete (unidentified)" — and no source field marks them machine-written."""
        sp = [species("bolete-unknown", "Bolete (unidentified)", "Boletaceae")]
        dropped, _, _ = plantdb.reconcile(sp, [], apply=True)
        self.assertEqual(dropped, [])
        self.assertEqual(len(sp), 1)

    def test_never_drops_the_target_of_a_field_check(self):
        sp = [auto("fern-colony", "Fern (unidentified colony)", "Polypodiopsida sp.")]
        o = [obs("1.jpg", "goldenrod",
                 verified={"status": "corrected", "species_id": "fern-colony",
                           "by": "J. Whitten"})]
        msgs = []
        dropped, _, _ = plantdb.reconcile(sp, o, apply=True, log=msgs.append)
        self.assertEqual(dropped, [], "that would delete the target of a field check")
        self.assertEqual(len(sp), 1)
        self.assertTrue(any("verified" in m for m in msgs), "and it must say so")

    def test_never_drops_the_unknown_sentinel(self):
        sp = [species("unknown", "Unidentified / Habitat shot", "—")]
        dropped, merged, _ = plantdb.reconcile(sp, [], apply=True)
        self.assertEqual((dropped, merged), ([], []))
        self.assertEqual(len(sp), 1)


class IsIdempotent(unittest.TestCase):

    def test_a_second_run_changes_nothing(self):
        sp = [auto("a", "Pixie Cup", "Cladonia sp."),
              auto("b", "Pixie Cup", "Cladonia sp."),
              auto("fern", "Fern (unidentified colony)", "Polypodiopsida sp.")]
        o = [obs("1.jpg", "a"), obs("2.jpg", "b"), obs("3.jpg", "fern")]
        plantdb.reconcile(sp, o, apply=True)
        dropped, merged, renames = plantdb.reconcile(sp, o, apply=True)
        self.assertEqual((dropped, merged, renames), ([], [], {}))

    def test_preview_does_not_mutate(self):
        sp = [auto("a", "Pixie Cup", "Cladonia sp."), auto("b", "Pixie Cup", "Cladonia sp.")]
        o = [obs("1.jpg", "a"), obs("2.jpg", "b")]
        before_sp, before_obs = [dict(s) for s in sp], [dict(x) for x in o]
        plantdb.reconcile(sp, o, apply=False)
        self.assertEqual(sp, before_sp)
        self.assertEqual(o, before_obs)


class ResolvesRetiredIds(unittest.TestCase):
    """A cached result or a stale match naming a merged-away id must land on the
    survivor, not recreate the duplicate."""

    def _ctx(self, sp):
        return {"species": sp, "by_id": {s["id"]: s for s in sp}, "catalogue": "",
                "args": type("A", (), {"model": "m"})(),
                "alias": {old: s["id"] for s in sp for old in s.get("merged_from") or []}}

    def test_cached_result_naming_a_retired_id(self):
        sp = [auto("pixie-cup-lichen", "Pixie Cup Lichen", "Cladonia sp.",
                   merged_from=["pixie-cup-trumpet"])]
        ctx, o = self._ctx(sp), {"file": "x.jpg"}
        from .context import identify, response
        identify.apply_result(response(common="Pixie Cup Lichen", scientific="Cladonia sp."),
                              o, ctx, cached_sid="pixie-cup-trumpet")
        self.assertEqual(o["species_id"], "pixie-cup-lichen")
        self.assertEqual(len(sp), 1, "and does not mint the duplicate back")

    def test_model_matching_a_retired_id(self):
        sp = [auto("pixie-cup-lichen", "Pixie Cup Lichen", "Cladonia sp.",
                   merged_from=["pixie-cup-trumpet"])]
        ctx, o = self._ctx(sp), {"file": "x.jpg"}
        from .context import identify, response
        identify.apply_result(
            response(matches_existing_id="pixie-cup-trumpet", common="", scientific=""),
            o, ctx)
        self.assertEqual(o["species_id"], "pixie-cup-lichen")
        self.assertEqual(len(sp), 1)


if __name__ == "__main__":
    unittest.main()
