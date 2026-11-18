"""Every branch of the decision layer. 'Our decision logic is unit-tested' is the
answer to the validation question, so these are the tests that have to exist."""
from datetime import datetime, timedelta, timezone

import pytest

from rules import (
    BLOCKED,
    HOLD_UNREADABLE,
    PHARMACIST_REVIEW,
    VERIFIED,
    Records,
    adjudicate,
    due_at,
    in_window,
    read_is_conflicted,
)

START = datetime(2026, 8, 22, 13, 0, tzinfo=timezone.utc)

MARIA = "MRN-482913"
MARTIN = "MRN-482931"


@pytest.fixture
def records():
    return Records.load("data", service_start=START)


def band(mrn=MARIA, text="GONZALES, MARIA | DOB 03/12/1948 | MRN 482913", **kw):
    w = {"text_vlm": text, "text_ocr": text, "mrn": mrn, "read_conflict": False}
    w.update(kw)
    return w


def cup(*pills, confidence=0.95):
    return [{"pill_id": p, "vlm_match": p, "attr_match": p, "confidence": confidence} for p in pills]


def contract(wristband=None, items=None):
    return {"wristband": wristband if wristband is not None else band(), "cup": items if items is not None else []}


# --- the green path -------------------------------------------------------

def test_correct_pill_correct_patient_in_window_verifies(records):
    now = START + timedelta(minutes=5)
    verdict, v = adjudicate(contract(band(), cup("P-LISIN10")), records, now)
    assert verdict == VERIFIED, v
    assert v == []


# --- the hero block: right drug, wrong patient ---------------------------

def test_martins_warfarin_over_marias_band_is_blocked(records):
    now = START + timedelta(minutes=15)
    verdict, v = adjudicate(contract(band(), cup("P-WARF5")), records, now)
    assert verdict == BLOCKED
    assert v[0].right == "patient"
    assert "MARTIN" in v[0].detail.upper()


# --- readability: the failure mode that must never pass silently ---------

def test_reader_disagreement_holds(records):
    w = band(text="GONZALES, MARIA")
    w["text_ocr"] = "GONZALES, MARTIN"
    verdict, v = adjudicate(contract(w, cup("P-LISIN10")), records, START)
    assert verdict == HOLD_UNREADABLE


def test_two_empty_reads_are_not_agreement(records):
    w = band(text="")
    w["text_ocr"] = ""
    assert read_is_conflicted(w) is True
    verdict, _ = adjudicate(contract(w, cup("P-LISIN10")), records, START)
    assert verdict == HOLD_UNREADABLE


def test_explicit_conflict_flag_holds(records):
    verdict, _ = adjudicate(contract(band(read_conflict=True), cup("P-LISIN10")), records, START)
    assert verdict == HOLD_UNREADABLE


def test_barcode_disagreeing_with_text_holds(records):
    verdict, _ = adjudicate(contract(band(barcode_mrn=MARTIN), cup("P-LISIN10")), records, START)
    assert verdict == HOLD_UNREADABLE


def test_empty_cup_holds_rather_than_verifies(records):
    verdict, v = adjudicate(contract(band(), []), records, START)
    assert verdict == HOLD_UNREADABLE
    assert v[0].right == "readability"


# --- identity ------------------------------------------------------------

def test_unknown_wristband_blocks(records):
    verdict, v = adjudicate(contract(band(mrn="MRN-000000"), cup("P-LISIN10")), records, START)
    assert verdict == BLOCKED
    assert v[0].right == "patient"


# --- timing --------------------------------------------------------------

def test_outside_window_blocks(records):
    now = START + timedelta(minutes=400)  # far past the +5min dose and its 60min window
    verdict, v = adjudicate(contract(band(), cup("P-LISIN10")), records, now)
    assert verdict == BLOCKED
    assert any(x.right == "time" for x in v)


def test_window_boundary_is_inclusive(records):
    order = next(o for o in records.orders if o["order_id"] == "ORD-1001")
    edge = due_at(order, START) + timedelta(minutes=int(order["window_min"]))
    assert in_window(order, START, edge) is True
    assert in_window(order, START, edge + timedelta(minutes=1)) is False


def test_naive_datetime_is_treated_as_utc_not_rejected(records):
    naive = (START + timedelta(minutes=5)).replace(tzinfo=None)
    verdict, _ = adjudicate(contract(band(), cup("P-LISIN10")), records, naive)
    assert verdict == VERIFIED


# --- dose ----------------------------------------------------------------

def test_double_dose_blocks(records):
    now = START + timedelta(minutes=5)
    verdict, v = adjudicate(contract(band(), cup("P-LISIN10", "P-LISIN10")), records, now)
    assert verdict == BLOCKED
    assert any(x.right == "dose" for x in v)


# --- perception uncertainty ----------------------------------------------

def test_low_confidence_escalates_instead_of_passing(records):
    now = START + timedelta(minutes=5)
    verdict, v = adjudicate(contract(band(), cup("P-LISIN10", confidence=0.42)), records, now)
    assert verdict == PHARMACIST_REVIEW
    assert "confidence" in v[0].detail


def test_confidence_floor_is_inclusive(records):
    now = START + timedelta(minutes=5)
    verdict, _ = adjudicate(contract(band(), cup("P-LISIN10", confidence=0.80)), records, now)
    assert verdict == VERIFIED


# --- determinism ---------------------------------------------------------

def test_same_input_same_output(records):
    now = START + timedelta(minutes=5)
    c = contract(band(), cup("P-LISIN10"))
    first = adjudicate(c, records, now)
    for _ in range(5):
        assert adjudicate(c, records, now)[0] == first[0]


def test_unordered_drug_blocks_on_drug_not_patient(records):
    now = START + timedelta(minutes=5)
    verdict, v = adjudicate(contract(band(), cup("P-NOT-A-REAL-DRUG")), records, now)
    assert verdict == BLOCKED
    assert v[0].right == "drug"
