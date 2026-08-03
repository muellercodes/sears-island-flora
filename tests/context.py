"""Make scripts/ importable from the tests, without installing anything.

Every function under test is pure — it takes species and observation lists and
returns a verdict — so the suite never touches data/, thumbs/, R2, the API, or the
sheet. That is deliberate: these tests have to be safe to run on a whim, including
against a working tree with real survey data in it.
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import identify  # noqa: E402,F401
import plantdb  # noqa: E402,F401
import sheets  # noqa: E402,F401


def species(id, common, scientific, **kw):
    """A catalogue entry. `source` marks it machine-written, as identify.py does."""
    return {"id": id, "common": common, "scientific": scientific, **kw}


def auto(id, common, scientific, **kw):
    return species(id, common, scientific, source="auto (claude-opus-5)", **kw)


def obs(file, species_id="unknown", **kw):
    """An observation. Located and dated by default — the tests that care about
    missing metadata say so explicitly, so the rest are not quietly testing it."""
    return {"file": file, "species_id": species_id,
            "lat": "44.449614", "lon": "-68.876122",
            "taken": "2024-09-05 13:58:43 +0000", **kw}


def response(**kw):
    """A model response, shaped like the identify schema."""
    base = {"is_survey_photo": True, "identifiable": True, "kind": "herb",
            "confidence": "medium", "common": "Goldenrod",
            "scientific": "Solidago canadensis"}
    return {**base, **kw}
