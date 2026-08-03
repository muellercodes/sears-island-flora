"""The bridge to the people who do the field verification.

Two classes of failure, and they need different defences. A bad *value* is caught by
the rules below and reported back to the steward. A bad *layout* cannot be caught
that way at all — every field is read by position, so a shifted column makes each
value plausible in the wrong place — and is refused wholesale.
"""
import unittest

from .context import plantdb, sheets

IDS = {"goldenrod", "dogbane"}


class WhatCountsAsAVerification(unittest.TestCase):
    """One definition, used by the pull to decide what to apply and by the push to
    write the reason into the sheet's `recorded?` column. If they disagreed, the
    sheet would tell someone their row was fine while the pipeline dropped it."""

    def ok(self, **v):
        self.assertIsNone(plantdb.verification_problem(v, IDS))

    def bad(self, expect, **v):
        problem = plantdb.verification_problem(v, IDS)
        self.assertIsNotNone(problem, f"should have been refused: {v!r}")
        self.assertIn(expect, problem)

    def test_blank_row_is_not_an_error(self):
        self.ok()
        self.ok(status="", by="")

    def test_accepts_a_signed_verdict(self):
        self.ok(status="confirmed", by="J. Whitten")
        self.ok(status="corrected", species_id="dogbane", by="J. Whitten")
        self.ok(status="rejected", by="J. Whitten")
        self.ok(status="revisit", by="J. Whitten")

    def test_refuses_an_unknown_status(self):
        self.bad("not one of", status="looks right to me", by="J. Whitten")

    def test_refuses_corrected_without_a_species(self):
        self.bad("needs an id", status="corrected", by="J. Whitten")

    def test_refuses_an_unknown_species_id(self):
        self.bad("unknown species id", status="corrected",
                 species_id="unicorn-moss", by="J. Whitten")

    def test_refuses_an_unattributed_verification(self):
        """The whole project turns on a person having gone and looked. A
        verification nobody signed is not a verification."""
        self.bad("verified by", status="confirmed")
        self.bad("verified by", status="corrected", species_id="dogbane")


class WhatCountsAsAReadableSheet(unittest.TestCase):

    def accepts(self, header, why=""):
        try:
            sheets.check_header(header)
        except SystemExit as e:
            self.fail(f"should have accepted ({why}): {str(e).splitlines()[0]}")

    def refuses(self, header, why=""):
        with self.assertRaises(SystemExit, msg=f"should have refused ({why})"):
            sheets.check_header(header)

    def test_accepts_the_current_layout(self):
        self.accepts(sheets.HEADERS)
        self.accepts(sheets.HEADERS + ["", ""], "Sheets pads trailing blanks")

    def test_accepts_a_sheet_written_before_a_column_was_added(self):
        """The columns it has are a correct prefix, so every index still points
        where it should and the next push appends the rest."""
        short = sheets.HEADERS[:sheets.FIRST_HUMAN + sheets.N_HUMAN]
        self.accepts(short, "older sheet, all human columns present")

    def test_refuses_a_prefix_that_stops_before_the_human_columns(self):
        """Otherwise every status reads blank, which reads as everyone
        withdrawing their verification at once."""
        self.refuses(sheets.HEADERS[:sheets.FIRST_HUMAN], "no human columns")

    def test_refuses_a_moved_column(self):
        H = sheets.HEADERS
        self.refuses([c for c in H if c != "latitude"], "pipeline column deleted")
        self.refuses([c for c in H if c != "verified by"], "human column deleted")
        self.refuses(H[:4] + ["elevation"] + H[4:], "column inserted")
        self.refuses(["STATUS"] + [c for c in H if c != "STATUS"], "columns reordered")

    def test_refuses_a_missing_or_renamed_header(self):
        self.refuses(["IMG_1.jpg", "", "2026-07-01"], "header row deleted")
        self.refuses(["filename"] + sheets.HEADERS[1:], "header renamed by hand")

    def test_names_every_column_that_moved(self):
        with self.assertRaises(SystemExit) as cm:
            sheets.check_header([c for c in sheets.HEADERS if c != "latitude"])
        message = str(cm.exception)
        self.assertIn("latitude", message)
        self.assertIn("Version history", message, "and says how to recover")


class LayoutInvariants(unittest.TestCase):
    """pull() reads by position. These are the assumptions those indices encode."""

    def test_human_columns_are_contiguous(self):
        owners = [o for _, o in sheets.COLUMNS]
        block = owners[sheets.FIRST_HUMAN:sheets.FIRST_HUMAN + sheets.N_HUMAN]
        self.assertEqual(set(block), {"human"})

    def test_pull_reads_five_human_fields(self):
        self.assertEqual(sheets.N_HUMAN, 5)

    def test_feedback_column_is_owned_by_the_pipeline_and_comes_last(self):
        self.assertEqual(sheets.COLUMNS[-1], ("recorded?", "pipeline"))


if __name__ == "__main__":
    unittest.main()
