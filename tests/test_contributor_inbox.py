"""Contributor mode: who may write what, and what a surplus mark is allowed to mean.

The site gained the ability to write to the survey, which makes two questions
load-bearing that were not before.

**Who may create a field check.** The whole project rests on `verified` recording
a real person on real ground. That rule is enforced in three places — the Worker,
this permission table, and `inbox-pull` — and the tests here are about the two
that live in this repository, because the third is a deployed artefact that can
change without any review.

**What marking a photograph surplus is permitted to destroy.** The mark is scoped
to one species at one place, but its effect is on the whole image, and a fifth of
this survey's photographs carry a second species in the background. So the tests
below pin the cases where a mark must be refused — most importantly the one where
accepting it would silently delete the only record of something else.
"""
import pathlib
import re
import unittest

from .context import plantdb, obs

IDS = {"willowherb", "rubus", "japanese-knotweed", "goldenrod"}
WORKER = pathlib.Path(__file__).resolve().parent.parent / "worker" / "index.js"


def near(file, species_id, metres_east=0.0, **kw):
    """An observation offset east of the others, so distances are exact and obvious.
    0.0000090 degrees of longitude at this latitude is almost exactly 0.7 m."""
    o = obs(file, species_id, **kw)
    o["lon"] = f"{-68.876122 + metres_east / (111_320 * 0.714):.7f}"
    return o


class WhoMayDoWhat(unittest.TestCase):
    """The permission table. Each of these encodes a decision, not a convention.

    A contributor may add photographs but not verdicts: an upload is screened,
    dated, located and identified by the same pipeline as anything else, so a bad
    one costs nothing, while a bad verification is a claim about ground truth.
    """

    def test_only_verifiers_and_admins_record_field_checks(self):
        self.assertTrue(plantdb.may("verifier", "verify"))
        self.assertTrue(plantdb.may("admin", "verify"))
        self.assertFalse(plantdb.may("contributor", "verify"))

    def test_everyone_signed_in_may_add_a_photograph(self):
        for role in ("contributor", "verifier", "admin"):
            self.assertTrue(plantdb.may(role, "upload"), role)

    def test_only_admins_mark_photographs_surplus(self):
        self.assertTrue(plantdb.may("admin", "redundant"))
        self.assertFalse(plantdb.may("verifier", "redundant"))
        self.assertFalse(plantdb.may("contributor", "redundant"))

    def test_the_unattended_token_cannot_manufacture_a_field_check(self):
        """The role the nightly run holds may collect the inbox and nothing else.

        This is the point of having a separate `pipeline` role at all. The token
        in GitHub Actions secrets runs unsupervised every two hours; if it could
        also write a verification, then the project's central claim would rest on
        a credential nobody is watching.
        """
        self.assertTrue(plantdb.may("pipeline", "drain"))
        for capability in ("verify", "redundant", "upload", "override"):
            self.assertFalse(plantdb.may("pipeline", capability), capability)

    def test_an_unknown_role_may_do_nothing(self):
        for capability in plantdb.CAPABILITIES:
            self.assertFalse(plantdb.may("", capability), capability)
            self.assertFalse(plantdb.may("steward", capability), capability)
            self.assertFalse(plantdb.may(None, capability), capability)

    def test_the_worker_carries_the_same_table(self):
        """worker/index.js holds a copy so it can refuse early with a useful
        message. A copy that drifts is worse than no copy: the site would offer a
        control that the pipeline then silently refuses, or — far worse — accept
        something the pipeline was relied on to catch. So they are compared."""
        src = WORKER.read_text()
        block = re.search(r"const CAPABILITIES = \{(.*?)\n\};", src, re.S)
        self.assertIsNotNone(block, "could not find CAPABILITIES in worker/index.js")
        found = {m.group(1): re.findall(r'"([a-z]+)"', m.group(2))
                 for m in re.finditer(r"(\w+):\s*\[([^\]]*)\]", block.group(1))}
        self.assertEqual({k: sorted(v) for k, v in found.items()},
                         {k: sorted(v) for k, v in plantdb.CAPABILITIES.items()},
                         "worker/index.js and plantdb.CAPABILITIES have drifted apart")


class WhatCountsAsASurplusMark(unittest.TestCase):
    """`redundancy_problem` — the boundary rule, used by the inbox drain, the CLI
    and `verify`. One definition, for the same reason `verification_problem` is
    one: a rule that decides what to accept must not be able to disagree with the
    thing that reports what was accepted."""

    def setUp(self):
        self.rep = near("rep.jpg", "willowherb")
        self.dup = near("dup.jpg", "willowherb", metres_east=2.0)
        self.mark = {"by": "Ada Admin", "of": "rep.jpg", "species_id": "willowherb"}

    def problem(self, mark=None, o=None, rep=None, obs_list=None):
        return plantdb.redundancy_problem(mark or self.mark, o or self.dup,
                                          self.rep if rep is None else rep,
                                          IDS, obs_list)

    def test_accepts_a_second_frame_of_the_same_patch(self):
        self.assertIsNone(self.problem())

    def test_refuses_an_unattributed_mark(self):
        self.assertIn("needs a name", self.problem({**self.mark, "by": ""}))

    def test_refuses_a_photograph_surplus_to_itself(self):
        self.assertIn("cannot be surplus to itself",
                      self.problem({**self.mark, "of": "dup.jpg"}, rep=self.dup))

    def test_refuses_deferring_to_a_frame_already_marked_surplus(self):
        """Otherwise a chain of marks leaves a find with no photograph at all."""
        self.rep["redundant"] = {"by": "Ada Admin", "of": "other.jpg",
                                 "species_id": "willowherb"}
        self.assertIn("itself marked surplus", self.problem())

    def test_refuses_across_the_patch_radius(self):
        """Beyond 10 m these are two finds, each entitled to its own photographs —
        the same threshold the whole survey is grouped by."""
        far = near("far.jpg", "willowherb", metres_east=25.0)
        self.assertIn("beyond the 10 m", self.problem(o=far))

    def test_refuses_when_the_two_are_not_currently_the_same_species(self):
        """Scoping is re-derived from the records as they stand, so a mark lapses
        if a re-identification moves either photograph rather than going on
        hiding a photograph of something else."""
        self.dup["species_id"] = "rubus"
        self.assertIn("must currently be records of", self.problem())

    def test_a_human_correction_decides_which_species_it_is(self):
        """`effective_species`, not the model's answer: if a person corrected the
        identification, the mark is scoped to what they said it is."""
        self.dup["species_id"] = "rubus"
        self.dup["verified"] = {"status": "corrected", "species_id": "willowherb",
                                "by": "V", "date": "2026-08-14"}
        self.assertIsNone(self.problem())

    def test_refuses_a_mark_that_would_delete_the_only_record_of_something_else(self):
        """The one that matters most.

        The mark says "surplus for willowherb", but dropping the frame drops
        everything in it. A plant caught behind the subject is a real record of it
        growing there, and for an invasive it may be the only record there will
        ever be. Refusing costs an explanation; accepting costs the evidence.
        """
        self.dup["also"] = ["japanese-knotweed"]
        problem = self.problem(obs_list=[self.rep, self.dup])
        self.assertIn("only photograph carrying 'japanese-knotweed'", problem)

    def test_allows_it_when_that_species_is_photographed_elsewhere_in_the_patch(self):
        self.dup["also"] = ["japanese-knotweed"]
        other = near("other.jpg", "rubus", metres_east=3.0, also=["japanese-knotweed"])
        self.assertIsNone(self.problem(obs_list=[self.rep, self.dup, other]))

    def test_a_surplus_frame_cannot_be_what_keeps_another_species_alive(self):
        """A frame already marked surplus is not carried, so it cannot be the
        'other photograph' that makes this mark safe."""
        self.dup["also"] = ["japanese-knotweed"]
        other = near("other.jpg", "rubus", metres_east=3.0, also=["japanese-knotweed"],
                     redundant={"by": "A", "of": "rep.jpg", "species_id": "rubus"})
        self.assertIn("only photograph carrying", self.problem(obs_list=[self.rep, self.dup, other]))


class WhatASurplusMarkDoesToTheSurvey(unittest.TestCase):
    """Marking is a presentation decision, never a deletion. The record, the
    original and the count of evidence all survive it."""

    def setUp(self):
        self.rep = near("rep.jpg", "willowherb")
        self.dup = near("dup.jpg", "willowherb", metres_east=2.0,
                        redundant={"by": "Ada Admin", "date": "2026-08-14",
                                   "of": "rep.jpg", "species_id": "willowherb"})

    def test_a_marked_frame_is_not_published(self):
        self.assertFalse(plantdb.is_publishable(self.dup))
        self.assertIn("marked surplus by Ada Admin", plantdb.withheld_reason(self.dup))

    def test_the_representative_still_is(self):
        self.assertTrue(plantdb.is_publishable(self.rep))

    def test_it_is_not_put_in_front_of_a_steward_again(self):
        """The find is already in the sheet under the photograph representing it.
        Seven rows for one patch is seven walks to verify one plant."""
        self.assertTrue(plantdb.reviewable(self.rep))
        self.assertFalse(plantdb.reviewable(self.dup))

    def test_the_find_still_counts_every_photograph_behind_it(self):
        """`n` is what the site can show; `surplus` is what a person set aside.
        Reporting only the first would make curation look like a thinner survey."""
        payload = plantdb.occurrence_payload([self.rep, self.dup])
        found = [x for x in payload if x["species_id"] == "willowherb"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["n"], 1)
        self.assertEqual(found[0]["surplus"], 1)

    def test_the_app_can_still_group_a_marked_frame_with_its_patch(self):
        """`files` keeps every member. A surplus frame missing from it detaches
        from its own find in the app and renders as a lone photograph elsewhere."""
        payload = plantdb.occurrence_payload([self.rep, self.dup])
        self.assertIn("dup.jpg", payload[0]["files"])

    def test_the_representative_is_never_a_marked_frame(self):
        """The anchor is what every list shows and what a steward is sent to check,
        so it has to be a photograph that is actually carried — regardless of which
        one happens to be earliest."""
        early = near("early.jpg", "willowherb", taken="2020-01-01 09:00:00 +0000",
                     redundant={"by": "A", "of": "rep.jpg", "species_id": "willowherb"})
        found = plantdb.occurrences([early, self.rep])
        self.assertEqual(found[0]["anchor"]["file"], "rep.jpg")

    def test_a_frame_withheld_on_its_own_merits_is_not_counted_as_curation(self):
        """`surplus_records` is 'kept off the site ONLY because someone marked it'.
        An undated photograph was never going to be published anyway, and counting
        it would overstate what the marking did."""
        undated = near("undated.jpg", "willowherb", metres_east=1.0, taken="",
                       redundant={"by": "A", "of": "rep.jpg", "species_id": "willowherb"})
        self.assertEqual([o["file"] for o in plantdb.surplus_records([self.rep, self.dup, undated])],
                         ["dup.jpg"])


class WhatTheDrainWillApply(unittest.TestCase):
    """`_sub_problem` — the pipeline re-deciding, on its own terms, what the Worker
    already accepted."""

    def setUp(self):
        self.rep = near("rep.jpg", "willowherb")
        self.dup = near("dup.jpg", "willowherb", metres_east=2.0)
        self.by_file = {o["file"]: o for o in (self.rep, self.dup)}

    def problem(self, **sub):
        return plantdb._sub_problem(sub, self.by_file, IDS)

    def test_applies_a_verification_from_a_verifier(self):
        self.assertIsNone(self.problem(kind="verify", role="verifier", by="V. Vera",
                                       file="rep.jpg", status="confirmed"))

    def test_refuses_a_verification_the_worker_should_never_have_accepted(self):
        """A contributor's verification is refused HERE even though it arrived
        with the Worker's blessing. The Worker is deployed separately and can be
        changed without review; this is the copy of the rule that cannot."""
        problem = self.problem(kind="verify", role="contributor", by="C. Con",
                               file="rep.jpg", status="confirmed")
        self.assertIn("may not verify", problem)

    def test_refuses_a_surplus_mark_from_a_verifier(self):
        self.assertIn("may not redundant",
                      self.problem(kind="redundant", role="verifier", by="V. Vera",
                                   file="dup.jpg", of="rep.jpg", species_id="willowherb"))

    def test_refuses_an_unnamed_submission(self):
        self.assertIn("no contributor name",
                      self.problem(kind="verify", role="verifier", by="",
                                   file="rep.jpg", status="confirmed"))

    def test_refuses_an_unknown_kind(self):
        self.assertIn("unknown submission kind",
                      self.problem(kind="delete", role="admin", by="A", file="rep.jpg"))

    def test_refuses_a_submission_against_a_record_that_does_not_exist(self):
        self.assertIn("no record named",
                      self.problem(kind="verify", role="verifier", by="V",
                                   file="ghost.jpg", status="confirmed"))

    def test_a_withdrawal_needs_no_verdict(self):
        """Someone may take back a check they recorded. `inbox-pull` still caps
        how many of those it will apply unattended."""
        self.assertIsNone(self.problem(kind="verify", role="verifier", by="V",
                                       file="rep.jpg", status=""))

    def test_refuses_unmarking_something_that_was_never_marked(self):
        self.assertIn("not marked surplus",
                      self.problem(kind="redundant", role="admin", by="A",
                                   file="dup.jpg", unmark=True))

    def test_survives_a_payload_that_is_nothing_like_a_submission(self):
        """It came off the network. It may be anything at all, and a crash in an
        unattended drain strands every other submission behind it."""
        for junk in ({}, {"kind": None}, {"kind": "verify", "role": "admin", "by": "A"},
                     {"kind": "redundant", "role": "admin", "by": "A", "file": "rep.jpg"}):
            self.assertIsNotNone(plantdb._sub_problem(junk, self.by_file, IDS), junk)


class WhatAnEditMayChange(unittest.TestCase):
    """The site can edit entries now that the steward sheet is gone. What it must
    never edit is the machine's own answer.

    `species_id`, `note` and `confidence` stay exactly as identification left them,
    however wrong they are, because the survey's claim rests on being able to show
    what the model said beside what a person found — and because `export-imap`
    names an Observer only where a human verdict exists. A correction is a
    `verified` record, not an overwrite.
    """

    def setUp(self):
        self.o = obs("a.jpg", "willowherb")

    def test_accepts_a_corrected_coordinate(self):
        self.assertIsNone(plantdb.edit_problem({"lat": "44.45", "lon": "-68.88"}, self.o))

    def test_refuses_to_overwrite_the_machines_identification(self):
        for field in ("species_id", "note", "confidence"):
            problem = plantdb.edit_problem({field: "anything"}, self.o)
            self.assertIn("never overwritten", problem or "", field)

    def test_refuses_half_a_coordinate(self):
        self.assertIn("as a pair", plantdb.edit_problem({"lat": "44.45"}, self.o))

    def test_refuses_a_coordinate_that_is_not_a_number(self):
        self.assertIn("not a number",
                      plantdb.edit_problem({"lat": "near the causeway", "lon": "-68.88"}, self.o))

    def test_refuses_an_impossible_coordinate(self):
        self.assertIn("outside", plantdb.edit_problem({"lat": "441.0", "lon": "-68.88"}, self.o))

    def test_an_edit_keeps_what_it_replaced(self):
        """A coordinate somebody typed and one a camera recorded are different
        kinds of claim, and the original has to be recoverable if the correction
        was itself wrong."""
        o = obs("a.jpg", "willowherb")
        plantdb._apply_submission(
            {"kind": "edit", "by": "Ada Admin", "date": "2026-08-15",
             "changes": {"lat": "44.4600", "lon": "-68.8700"}}, o)
        self.assertEqual(o["lat"], "44.46")
        self.assertEqual(o["location_source"], "corrected-by-hand")
        self.assertEqual(o["location_was"]["lat"], "44.449614")

    def test_a_curator_note_sits_beside_the_models_note_not_over_it(self):
        o = obs("a.jpg", "willowherb", note="Dense clonal stand, model description.")
        plantdb._apply_submission(
            {"kind": "edit", "by": "Ada Admin", "changes": {"curator_note": "Cut back in 2025."}}, o)
        self.assertEqual(o["note"], "Dense clonal stand, model description.")
        self.assertEqual(o["curator_note"]["text"], "Cut back in 2025.")
        self.assertEqual(o["curator_note"]["by"], "Ada Admin")


class WithdrawingARecord(unittest.TestCase):
    """Soft, reversible and attributed — the site can take a record down but not
    destroy it. A mis-tap on a phone must not be able to lose evidence."""

    def test_a_withdrawal_needs_a_reason(self):
        self.assertIn("needs a reason", plantdb.withdrawal_problem({"by": "Ada Admin"}))
        self.assertIn("needs a reason",
                      plantdb.withdrawal_problem({"by": "Ada Admin", "reason": "   "}))

    def test_a_withdrawal_needs_a_name(self):
        self.assertIn("needs a name", plantdb.withdrawal_problem({"reason": "a duplicate"}))

    def test_a_withdrawn_record_leaves_the_site_but_not_the_data(self):
        o = obs("a.jpg", "willowherb",
                withdrawn={"by": "Ada Admin", "date": "2026-08-15", "reason": "somebody's dog"})
        self.assertTrue(plantdb.is_withdrawn(o))
        self.assertFalse(plantdb.is_publishable(o))
        self.assertIn("withdrawn by Ada Admin", plantdb.withheld_reason(o))
        self.assertIn("somebody's dog", plantdb.withheld_reason(o))

    def test_restoring_puts_it_back(self):
        o = obs("a.jpg", "willowherb",
                withdrawn={"by": "Ada Admin", "date": "2026-08-15", "reason": "wrong"})
        plantdb._apply_submission({"kind": "restore", "by": "Ada Admin"}, o)
        self.assertFalse(plantdb.is_withdrawn(o))
        self.assertTrue(plantdb.is_publishable(o))

    def test_a_withdrawn_record_is_not_part_of_any_find(self):
        """Otherwise it still anchors an occurrence, and a find that was taken
        down goes on being listed under a photograph nobody can see."""
        a = obs("a.jpg", "willowherb")
        b = obs("b.jpg", "willowherb",
                withdrawn={"by": "A", "date": "2026-08-15", "reason": "x"})
        found = plantdb.occurrences([a, b])
        self.assertEqual([m["file"] for m in found[0]["members"]], ["a.jpg"])


class TheArchiveIsNotThePhotosFolder(unittest.TestCase):
    """Every "nothing is deleted" promise has to name somewhere the original
    really is. photos/ is emptied with the runner on every cloud pipeline run, so
    for an uploaded photograph it was never the answer."""

    def test_an_uploaded_original_is_in_the_inbox_bucket(self):
        o = obs("a.jpg", "willowherb", archive_key="originals/abc-123")
        self.assertIn("contributor inbox bucket", plantdb.archive_of(o))

    def test_a_drive_photograph_is_archived_in_drive(self):
        self.assertIn("Drive", plantdb.archive_of(obs("a.jpg", drive_id="1AbC")))

    def test_a_record_with_no_archive_says_so_rather_than_guessing(self):
        self.assertIsNone(plantdb.archive_of(obs("a.jpg")))


class AddingASpeciesFromTheSite(unittest.TestCase):
    """A correction needs something to correct TO, so admins can create catalogue
    entries. Those entries must survive `reconcile`, which drops machine-created
    ones — an unattended run deleting an editorial decision is exactly what the
    `source` field exists to prevent."""

    def problem(self, **sub):
        return plantdb._sub_problem({"kind": "species", "role": "admin", "by": "A", **sub},
                                    {}, IDS)

    def test_accepts_a_new_entry(self):
        self.assertIsNone(self.problem(species_id="sea-rocket", common="Sea Rocket"))

    def test_refuses_an_id_that_already_exists(self):
        self.assertIn("already in the catalogue",
                      self.problem(species_id="willowherb", common="Willowherb"))

    def test_refuses_an_id_that_is_not_an_id(self):
        for bad in ("Sea Rocket", "sea_rocket", "", "a"):
            self.assertIsNotNone(self.problem(species_id=bad, common="Sea Rocket"), bad)

    def test_refuses_an_entry_with_no_common_name(self):
        self.assertIn("common name", self.problem(species_id="sea-rocket", common=""))


class WhereAVerificationCameFrom(unittest.TestCase):
    """`via` — added because the pull could silently withdraw checks it had never
    recorded.

    The sheet's blank STATUS column means "withdraw". A verification made at a
    terminal or in the field app was never written into that column, so a blank
    cell against it is the sheet not having caught up — and without this
    distinction the next unattended pull retracted it, usually within two hours.
    """

    def test_a_verification_defaults_to_having_come_from_the_sheet(self):
        """Records written before `via` existed keep behaving exactly as they did."""
        self.assertEqual((obs("a.jpg", verified={"status": "confirmed", "by": "J"})
                          )["verified"].get("via", "sheet"), "sheet")

    def test_the_three_channels_are_distinguishable(self):
        for via in ("sheet", "cli", "site"):
            o = obs("a.jpg", verified={"status": "confirmed", "by": "J", "via": via})
            self.assertTrue(plantdb.is_verified(o))
            self.assertEqual(o["verified"]["via"], via)


class WhereAnUploadGetsItsLocation(unittest.TestCase):
    """Two ways, and neither is a guess: the photograph carries its own, or it was
    attached to a find that already exists and inherits that find's coordinates.

    There used to be a third — the phone's fix at upload time — and it was wrong.
    It records where the photographer was standing when they pressed send, so
    anyone who walked the island and uploaded that evening would have had their
    kitchen recorded as the find.
    """

    def test_an_attached_photograph_inherits_the_finds_coordinates(self):
        rec = {"lat": "", "lon": "", "taken": ""}
        plantdb._apply_fallback(rec, {"lat": "44.4712", "lon": "-68.8834",
                                      "source": "attached-to-find", "of": "rep.jpg",
                                      "taken": "2026-08-14T14:05:00"})
        self.assertEqual(rec["lat"], "44.4712")
        self.assertEqual(rec["location_source"], "attached-to-find")
        self.assertEqual(rec["location_from"], "rep.jpg")

    def test_the_photographs_own_coordinates_always_win(self):
        """And nothing is stamped, because nothing was substituted — a record that
        claimed an inherited location it had not used would be the same class of
        error as inventing a date."""
        rec = {"lat": "44.4500", "lon": "-68.8800", "taken": "2026-08-01T09:00:00"}
        plantdb._apply_fallback(rec, {"lat": "44.4712", "lon": "-68.8834",
                                      "source": "attached-to-find"})
        self.assertEqual(rec["lat"], "44.4500")
        self.assertNotIn("location_source", rec)

    def test_half_a_coordinate_pair_is_never_used(self):
        """One source's latitude with another's longitude would place the record
        somewhere neither of them saw."""
        rec = {"lat": "", "lon": "", "taken": ""}
        plantdb._apply_fallback(rec, {"lat": "44.4712", "lon": "", "source": "attached-to-find"})
        self.assertEqual(rec["lat"], "")
        self.assertNotIn("location_source", rec)

    def test_a_record_with_no_location_is_still_withheld(self):
        """The upload gate refuses these outright, but the derived rule stays as
        the backstop for every other way a record can arrive."""
        o = obs("field.jpg", "unknown", lat="", lon="", submitted_by="C. Con")
        self.assertIn("no location", plantdb.withheld_reason(o))


if __name__ == "__main__":
    unittest.main()
