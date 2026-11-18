"""The memory layer's safety contract: escalates on a hot pattern, NEVER downgrades,
and the counterfactual toggle really is a no-op. Runs against the live local Mongo
in an isolated test database."""
from datetime import datetime, timedelta, timezone

import pytest

import memory
from db import ensure_schema, get_db

NOW = datetime.now(timezone.utc)


@pytest.fixture
def db():
    d = get_db("medpass_test")
    d.drop_collection("near_misses")
    ensure_schema(d)
    rows = [
        {"nm_id": f"NM-T{i}", "ts": NOW - timedelta(days=3 * i + 1), "type": "WRONG_PATIENT",
         "drug": "Warfarin", "ward": "4W", "shift": "night", "note": "test cluster",
         "outcome": "blocked_before_administration", "provenance": "seeded_synthetic"}
        for i in range(6)  # 6 >= threshold of 5
    ]
    # One stale row outside the 90-day window - must not count.
    rows.append({**rows[0], "nm_id": "NM-TOLD", "ts": NOW - timedelta(days=200)})
    d.near_misses.insert_many(rows)
    yield d
    memory.set_enabled(True)


def test_hot_pattern_escalates_verified(db):
    r = memory.escalate(db, "VERIFIED", ["Warfarin"], "4W", NOW)
    assert r["escalated"] is True
    assert r["verdict"] == "PHARMACIST_REVIEW"
    assert r["patterns"][0]["count"] == 6  # stale row excluded
    assert "Warfarin" in r["reason"]


def test_cold_drug_passes_through(db):
    r = memory.escalate(db, "VERIFIED", ["Lisinopril"], "4W", NOW)
    assert r["escalated"] is False
    assert r["verdict"] == "VERIFIED"


def test_other_ward_is_not_hot(db):
    r = memory.escalate(db, "VERIFIED", ["Warfarin"], "3E", NOW)
    assert r["escalated"] is False


def test_never_downgrades_blocked(db):
    for v in ("BLOCKED", "HOLD_UNREADABLE", "PHARMACIST_REVIEW"):
        r = memory.escalate(db, v, ["Warfarin"], "4W", NOW)
        assert r["verdict"] == v
        assert r["escalated"] is False


def test_memory_off_is_a_true_noop(db):
    memory.set_enabled(False)
    r = memory.escalate(db, "VERIFIED", ["Warfarin"], "4W", NOW)
    assert r["verdict"] == "VERIFIED"
    assert r["escalated"] is False
    assert r["memory_enabled"] is False


def test_evidence_carries_pipeline_and_stats(db):
    p = memory.risk_pattern(db, "Warfarin", "4W", NOW)
    assert p["hot"] is True
    assert p["pipeline"][0]["$match"]["drug"] == "Warfarin"
    assert p["query_ms"] >= 0
    assert p["by_shift"].get("night") == 6
