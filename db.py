"""MEDPASS persistence layer - MongoDB is the ONLY runtime source of truth.

Everything the agent knows lives here: clinical records, the verification stream,
near-miss memory, and a tamper-evident hash-chained ledger. Kill the process and
nothing is lost - that is the point (the agent survives its own sandbox).

Single-node replica set (rs0) so change streams AND transactions are real.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone

from pymongo import ASCENDING, DESCENDING, MongoClient

MONGO_URI = os.environ.get("MEDPASS_MONGO", "mongodb://localhost:27017/?replicaSet=rs0&directConnection=true")
DB_NAME = os.environ.get("MEDPASS_DB", "medpass")

_client: MongoClient | None = None


def client() -> MongoClient:
    global _client
    if _client is None:
        _client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=4000)
    return _client


def get_db(name: str = DB_NAME):
    return client()[name]


# --------------------------------------------------------------------------
# Schema: validators where supported, native time-series for administrations
# --------------------------------------------------------------------------

NEAR_MISS_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["ts", "type", "ward", "shift", "provenance"],
        "properties": {
            "type": {"enum": ["WRONG_DRUG", "WRONG_PATIENT", "WRONG_DOSE", "WRONG_TIME", "UNREADABLE"]},
            "shift": {"enum": ["day", "evening", "night"]},
            "provenance": {"enum": ["seeded_synthetic", "observed_today"]},
        },
    }
}

VERIFICATION_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["event_id", "ts", "verdict", "provenance"],
        "properties": {
            "verdict": {"enum": ["VERIFIED", "BLOCKED", "PHARMACIST_REVIEW", "HOLD_UNREADABLE"]},
            "provenance": {"enum": ["seeded_synthetic", "observed_today", "replay_fixture"]},
        },
    }
}

LEDGER_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["seq", "ts", "actor", "action", "allowed", "prev_hash", "hash"],
        "properties": {"allowed": {"bsonType": "bool"}},
    }
}


def ensure_schema(db) -> None:
    """Idempotent. Creates collections, validators, indexes, TTL, time-series."""
    names = set(db.list_collection_names())

    if "administrations" not in names:
        # Native time-series: this is 12 months of event data, so we store it in the
        # primitive MongoDB built for it (no validators/change streams by design there).
        db.create_collection(
            "administrations",
            timeseries={"timeField": "ts", "metaField": "meta", "granularity": "hours"},
        )

    for coll, validator in (
        ("near_misses", NEAR_MISS_VALIDATOR),
        ("verifications", VERIFICATION_VALIDATOR),
        ("ledger", LEDGER_VALIDATOR),
    ):
        if coll not in names:
            db.create_collection(coll, validator=validator)

    for coll in ("patients", "med_orders", "pill_library", "approvals", "pending_approvals", "state"):
        if coll not in names:
            db.create_collection(coll)

    db.near_misses.create_index([("drug", ASCENDING), ("ward", ASCENDING), ("ts", DESCENDING)], name="risk_lookup")
    db.near_misses.create_index([("ts", DESCENDING)])
    db.verifications.create_index([("event_id", ASCENDING)], unique=True)
    db.verifications.create_index([("ts", DESCENDING)])
    db.ledger.create_index([("seq", ASCENDING)], unique=True)
    db.patients.create_index([("mrn", ASCENDING)], unique=True)
    db.med_orders.create_index([("mrn", ASCENDING)])
    db.pill_library.create_index([("pill_id", ASCENDING)], unique=True)
    # Unattended approval requests do not linger forever.
    db.pending_approvals.create_index([("ts", ASCENDING)], expireAfterSeconds=3600, name="ttl_1h")


# --------------------------------------------------------------------------
# Tamper-evident ledger: hash chain, appended inside a transaction
# --------------------------------------------------------------------------

GENESIS = "0" * 64


def _canon(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _row_hash(seq: int, ts: str, actor: str, action: str, allowed: bool, payload_hash: str, prev_hash: str) -> str:
    material = f"{seq}|{ts}|{actor}|{action}|{int(allowed)}|{payload_hash}|{prev_hash}"
    return hashlib.sha256(material.encode()).hexdigest()


def ledger_append(db, actor: str, action: str, allowed: bool, payload: dict, event_id: str | None = None) -> dict:
    """Append one row to the hash chain. Transaction keeps seq/prev_hash consistent.

    Concurrent appenders (the API and the change-stream agent) can race on the next
    seq; Mongo reports that as a TransientTransactionError and expects a retry."""
    from pymongo.errors import DuplicateKeyError, OperationFailure

    for attempt in range(8):
        try:
            with client().start_session() as s:
                with s.start_transaction():
                    last = db.ledger.find_one(sort=[("seq", DESCENDING)], session=s)
                    seq = (last["seq"] + 1) if last else 1
                    prev_hash = last["hash"] if last else GENESIS
                    ts = datetime.now(timezone.utc).isoformat()
                    payload_hash = hashlib.sha256(_canon(payload).encode()).hexdigest()
                    row = {
                        "seq": seq,
                        "ts": ts,
                        "actor": actor,
                        "action": action,
                        "allowed": allowed,
                        "event_id": event_id,
                        "payload": payload,
                        "payload_hash": payload_hash,
                        "prev_hash": prev_hash,
                        "hash": _row_hash(seq, ts, actor, action, allowed, payload_hash, prev_hash),
                    }
                    db.ledger.insert_one(row, session=s)
            row.pop("_id", None)
            return row
        except (OperationFailure, DuplicateKeyError) as e:
            transient = isinstance(e, DuplicateKeyError) or (
                hasattr(e, "has_error_label") and e.has_error_label("TransientTransactionError"))
            if not transient or attempt == 7:
                raise
            time.sleep(0.02 * (attempt + 1))


def verify_chain(db) -> dict:
    """Walk the whole chain. Returns ok + where it breaks, if it breaks."""
    prev = GENESIS
    n = 0
    for row in db.ledger.find(sort=[("seq", ASCENDING)]):
        n += 1
        expected_payload = hashlib.sha256(_canon(row["payload"]).encode()).hexdigest()
        expected = _row_hash(row["seq"], row["ts"], row["actor"], row["action"], row["allowed"], row["payload_hash"], row["prev_hash"])
        if row["prev_hash"] != prev or row["hash"] != expected or row["payload_hash"] != expected_payload:
            return {"ok": False, "rows": n, "broken_at_seq": row["seq"]}
        prev = row["hash"]
    return {"ok": True, "rows": n, "broken_at_seq": None}


def watch_verifications(db):
    """Change stream: MongoDB itself drives the agent loop - no polling."""
    return db.verifications.watch([{"$match": {"operationType": "insert"}}], full_document="updateLookup")
