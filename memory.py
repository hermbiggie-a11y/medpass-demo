"""MEDPASS memory layer - retrieval that CHANGES BEHAVIOR.

After the deterministic rules pass a cup, this layer asks MongoDB what it remembers
about the drugs involved on this ward. If the near-miss history shows a live risk
pattern, the verdict is escalated to PHARMACIST_REVIEW - same cup, same rules,
different outcome, because of what the database remembers.

Two properties keep the safety story intact:
  * Escalation is deterministic DB retrieval - never model output.
  * It can only ever ADD human review. It never downgrades a verdict.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

RISK_WINDOW_DAYS = 90
RISK_THRESHOLD = 5  # near-misses within the window that make a drug "hot" on a ward

# Flipped from the UI for the counterfactual beat: MEMORY OFF -> same cup verifies.
MEMORY_ENABLED = True


def set_enabled(on: bool) -> None:
    global MEMORY_ENABLED
    MEMORY_ENABLED = bool(on)


def _pipeline(drug: str, ward: str, cutoff: datetime) -> list[dict]:
    return [
        {"$match": {"drug": drug, "ward": ward, "ts": {"$gte": cutoff}}},
        {"$facet": {
            "total": [{"$count": "n"}],
            "by_shift": [{"$group": {"_id": "$shift", "n": {"$sum": 1}}}, {"$sort": {"n": -1}}],
            "by_type": [{"$group": {"_id": "$type", "n": {"$sum": 1}}}],
            "recent": [{"$sort": {"ts": -1}}, {"$limit": 3},
                       {"$project": {"_id": 0, "nm_id": 1, "ts": 1, "note": 1, "shift": 1, "provenance": 1}}],
        }},
    ]


def _explain_stats(db, match: dict) -> dict:
    """Real executionStats for the retrieval core - the UI shows these, measured."""
    try:
        ex = db.command("explain",
                        {"aggregate": "near_misses", "pipeline": [{"$match": match}], "cursor": {}},
                        verbosity="executionStats")
        stats = ex.get("executionStats", {}) or ex.get("stages", [{}])[0].get("$cursor", {}).get("executionStats", {})
        plan = ex.get("queryPlanner", {}).get("winningPlan", {})
        index = None
        node = plan
        while isinstance(node, dict):
            if node.get("indexName"):
                index = node["indexName"]
                break
            node = node.get("inputStage") or (node.get("inputStages") or [None])[0] or node.get("queryPlan")
        return {
            "index": index,
            "docs_examined": stats.get("totalDocsExamined"),
            "keys_examined": stats.get("totalKeysExamined"),
            "returned": stats.get("nReturned"),
        }
    except Exception as e:  # explain must never break a verification
        return {"error": str(e)[:120]}


def risk_pattern(db, drug: str, ward: str, now: datetime | None = None) -> dict:
    """One drug on one ward: is there a live near-miss pattern? Returns the evidence
    the UI renders - counts, shift breakdown, sample events, pipeline, explain, ms."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=RISK_WINDOW_DAYS)
    pipeline = _pipeline(drug, ward, cutoff)

    t0 = time.perf_counter()
    facet = list(db.near_misses.aggregate(pipeline))[0]
    ms = (time.perf_counter() - t0) * 1000

    total = facet["total"][0]["n"] if facet["total"] else 0
    return {
        "drug": drug,
        "ward": ward,
        "window_days": RISK_WINDOW_DAYS,
        "count": total,
        "threshold": RISK_THRESHOLD,
        "hot": total >= RISK_THRESHOLD,
        "by_shift": {r["_id"]: r["n"] for r in facet["by_shift"]},
        "by_type": {r["_id"]: r["n"] for r in facet["by_type"]},
        "recent": [{**r, "ts": r["ts"].isoformat() if hasattr(r["ts"], "isoformat") else r["ts"]}
                   for r in facet["recent"]],
        "pipeline": pipeline,
        "explain": _explain_stats(db, pipeline[0]["$match"]),
        "query_ms": round(ms, 2),
    }


def escalate(db, verdict: str, cup_drugs: list[str], ward: str, now: datetime | None = None) -> dict:
    """The behavior change. VERIFIED + a hot pattern -> PHARMACIST_REVIEW, with the
    retrieved evidence attached. Anything else passes through untouched."""
    result = {
        "verdict": verdict,
        "escalated": False,
        "memory_enabled": MEMORY_ENABLED,
        "patterns": [],
        "reason": None,
    }
    if not MEMORY_ENABLED or verdict != "VERIFIED":
        return result

    for drug in dict.fromkeys(cup_drugs):  # de-dupe, keep order
        p = risk_pattern(db, drug, ward, now)
        result["patterns"].append(p)
        if p["hot"]:
            top_shift = max(p["by_shift"], key=p["by_shift"].get) if p["by_shift"] else "?"
            result["verdict"] = "PHARMACIST_REVIEW"
            result["escalated"] = True
            result["reason"] = (
                f"{drug} has {p['count']} near-misses on ward {ward} in the last "
                f"{p['window_days']} days (most on {top_shift} shift) - "
                f"memory requires pharmacist review"
            )
            break
    return result
