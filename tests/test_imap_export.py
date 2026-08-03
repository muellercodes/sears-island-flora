"""Eligibility for the iMapInvasives bulk upload.

The state dataset is used by people making land-management decisions, so what
leaves this project for it has to clear a higher bar than what goes on the site.
The rule is the project's own — nothing is a finding until a person has confirmed
it on the ground — and it happens to supply the one field iMap requires that the
survey does not otherwise collect: the Observer.
"""
import unittest

from .context import obs, plantdb, species

REGULATED = species("knotweed", "Japanese Knotweed", "Reynoutria japonica",
                    origin_status="regulated")
INVASIVE = species("coltsfoot", "Coltsfoot", "Tussilago farfara", origin_status="invasive")
NATIVE = species("goldenrod", "Goldenrod", "Solidago canadensis", origin_status="native")

CONFIRMED = {"status": "confirmed", "by": "J. Whitten", "date": "2026-08-10"}


class WhatMayGoToTheState(unittest.TestCase):

    def test_a_confirmed_regulated_find_is_eligible(self):
        o = obs("a.jpg", "knotweed", verified=CONFIRMED)
        self.assertIsNone(plantdb.imap_blocker(o, REGULATED))

    def test_a_corrected_find_is_eligible(self):
        """The human overruled the model and named something else. That is still
        somebody standing in front of the plant."""
        o = obs("a.jpg", "goldenrod",
                verified={"status": "corrected", "species_id": "coltsfoot",
                          "by": "R. Olson", "date": "2026-08-10"})
        self.assertIsNone(plantdb.imap_blocker(o, INVASIVE))


class WhatMayNot(unittest.TestCase):

    def blocked(self, o, sp, expect):
        why = plantdb.imap_blocker(o, sp)
        self.assertIsNotNone(why, "should have been blocked")
        self.assertIn(expect, why)

    def test_an_unverified_machine_identification(self):
        """The whole point. A confident model answer is still a lead, and putting
        leads into a state dataset that managers act on is the failure this
        project exists to avoid."""
        self.blocked(obs("a.jpg", "knotweed"), REGULATED, "not field-verified")

    def test_a_verification_nobody_signed(self):
        o = obs("a.jpg", "knotweed", verified={"status": "confirmed", "by": ""})
        self.blocked(o, REGULATED, "no observer")

    def test_a_find_the_person_rejected(self):
        o = obs("a.jpg", "knotweed", verified={"status": "rejected", "by": "J. Whitten"})
        self.blocked(o, REGULATED, "rejected")

    def test_a_find_still_awaiting_another_look(self):
        o = obs("a.jpg", "knotweed", verified={"status": "revisit", "by": "J. Whitten"})
        self.blocked(o, REGULATED, "not settled")

    def test_a_native_species(self):
        """iMapInvasives is an invasive species database. A confirmed goldenrod is
        good survey data and noise to the people receiving this."""
        o = obs("a.jpg", "goldenrod", verified=CONFIRMED)
        self.blocked(o, NATIVE, "iMapInvasives tracks invasives")

    def test_a_screened_out_photograph(self):
        o = obs("a.jpg", "knotweed", rejected=True, verified=CONFIRMED)
        self.blocked(o, REGULATED, "screened out")

    def test_a_record_with_no_coordinates(self):
        o = {**obs("a.jpg", "knotweed", verified=CONFIRMED), "lat": "", "lon": ""}
        self.blocked(o, REGULATED, "no coordinates")

    def test_a_species_missing_from_the_catalogue(self):
        self.blocked(obs("a.jpg", "ghost", verified=CONFIRMED), None, "not in the catalogue")


class TheColumnsMatchTheDocumentedSpec(unittest.TestCase):
    """Required column names come from NatureServe's published bulk-upload spec.
    If these drift, an upload is handed back."""

    def test_required_columns(self):
        self.assertEqual(plantdb.IMAP_REQUIRED,
                         ["Source Unique ID", "Species", "Date", "Observer",
                          "Latitude", "Longitude"])

    def test_only_invasive_and_regulated_are_tracked(self):
        self.assertEqual(set(plantdb.IMAP_TRACKED), {"invasive", "regulated"})


if __name__ == "__main__":
    unittest.main()
