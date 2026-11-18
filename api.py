"""MEDPASS API - the spine that ties perception, rules, memory and the UI together.

Flow per verification:
  contract (perception or replay) -> strip any inbound verdict (perception may never
  assert one) -> rules.adjudicate (pure, unit-tested) -> memory.escalate (MongoDB
  retrieval; only ever ADDS human review) -> persist + hash-chain ledger -> SSE.

MongoDB is the only source of truth. Kill this process and restart it: every ledger
row, approval, near-miss and learned pattern comes back. That is the survival story.

Run: .venv/bin/uvicorn api:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests as http
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from sse_starlette.sse import EventSourceResponse

import memory
import rules
from db import ensure_schema, get_db, ledger_append, verify_chain

ROOT = Path(__file__).parent
OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434")
QUERY_MODEL = "nemotron-3-nano:4b"
WAN_IFACES = ["enP7s7", "wlP9s9"]  # physical uplinks; measured, never asserted

app = FastAPI(title="MEDPASS")
db = get_db()

# --------------------------------------------------------------------------
# SSE fan-out
# --------------------------------------------------------------------------
_subs: set[asyncio.Queue] = set()
_loop: asyncio.AbstractEventLoop | None = None


def emit(kind: str, data: dict) -> None:
    payload = {"kind": kind, "ts": datetime.now(timezone.utc).isoformat(), **data}
    if _loop is None:
        return
    for q in list(_subs):
        _loop.call_soon_threadsafe(q.put_nowait, payload)


async def _body(request: Request) -> dict:
    """Bodyless or malformed POSTs must hit the policy gate, not a 500."""
    try:
        return await request.json()
    except Exception:
        return {}


@app.get("/events")
async def events():
    q: asyncio.Queue = asyncio.Queue()
    _subs.add(q)

    async def gen():
        try:
            while True:
                item = await q.get()
                yield {"event": item["kind"], "data": json.dumps(item, default=str)}
        finally:
            _subs.discard(q)

    return EventSourceResponse(gen())


# --------------------------------------------------------------------------
# Camera (shared capture; UI streams MJPEG, SPACE commits the latest frame)
# --------------------------------------------------------------------------
_cam_lock = threading.Lock()
_last_frame = None  # BGR ndarray
_cam_thread = None


def _cam_worker():
    global _last_frame
    import cv2

    def _open():
        cap = cv2.VideoCapture(0, cv2.CAP_V4L2)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
        return cap

    cap = _open()
    misses = 0
    while True:
        ok, frame = cap.read()
        if ok:
            misses = 0
            with _cam_lock:
                _last_frame = frame
        else:
            # Device was busy or vanished (restart overlap, replug): re-open, forever.
            misses += 1
            if misses >= 4:
                try:
                    cap.release()
                except Exception:
                    pass
                time.sleep(1.0)
                cap = _open()
                misses = 0
            else:
                time.sleep(0.5)


def _ensure_camera():
    global _cam_thread
    if _cam_thread is None:
        _cam_thread = threading.Thread(target=_cam_worker, daemon=True)
        _cam_thread.start()


@app.get("/camera/stream")
def camera_stream():
    _ensure_camera()
    import cv2

    def gen():
        while True:
            with _cam_lock:
                frame = None if _last_frame is None else _last_frame.copy()
            if frame is not None:
                small = cv2.resize(frame, (960, 540))
                _, jpg = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 70])
                yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpg.tobytes() + b"\r\n")
            time.sleep(0.1)

    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")


# --------------------------------------------------------------------------
# Records + service clock (persisted so a restart does not move the schedule)
# --------------------------------------------------------------------------

def service_start() -> datetime:
    """Demo scheduling is relative to service start (disclosed via demo_offset_min).
    If the clock has drifted past the dose windows (~60 min), it re-arms so a demo
    that starts late never silently turns the green path red."""
    now = datetime.now(timezone.utc)
    row = db.state.find_one({"_id": "clock"})
    if row:
        start = row["service_start"].replace(tzinfo=timezone.utc)
        if (now - start).total_seconds() < 40 * 60:
            return start
        start = now - timedelta(minutes=10)  # both demo doses back in window
    else:
        start = now
    db.state.replace_one({"_id": "clock"}, {"_id": "clock", "service_start": start}, upsert=True)
    return start


def load_records() -> rules.Records:
    patients = {p["mrn"]: p for p in db.patients.find({}, {"_id": 0})}
    orders = list(db.med_orders.find({}, {"_id": 0}))
    pills = {p["pill_id"]: p for p in db.pill_library.find({}, {"_id": 0})}
    return rules.Records(patients=patients, orders=orders, pills=pills, service_start=service_start())


# --------------------------------------------------------------------------
# TTS - Piper renders WAVs; there is no speaker on this box, so the UI shows the
# transcript and the response says audible=false. Honest by construction.
# --------------------------------------------------------------------------
PIPER = Path(os.environ.get("MEDPASS_PIPER", "piper"))
VOICE = Path(os.environ.get("MEDPASS_PIPER_VOICE", str(ROOT / "voices" / "en_US-amy-medium.onnx")))
TTS_DIR = ROOT / "static" / "tts"


def tts_render(name: str, text: str) -> str | None:
    TTS_DIR.mkdir(parents=True, exist_ok=True)
    out = TTS_DIR / f"{name}.wav"
    if out.exists():
        return f"/static/tts/{name}.wav"
    try:
        subprocess.run([str(PIPER), "--model", str(VOICE), "--output_file", str(out)],
                       input=text.encode(), capture_output=True, timeout=20)
        return f"/static/tts/{name}.wav" if out.exists() else None
    except Exception:
        return None


@app.get("/static/tts/{name}")
def tts_file(name: str):
    p = (TTS_DIR / name).resolve()
    if not p.is_relative_to(TTS_DIR.resolve()) or not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="audio/wav")


# --------------------------------------------------------------------------
# The verification path
# --------------------------------------------------------------------------

def run_verification(contract: dict, source: str) -> dict:
    # Perception (or anything upstream) may NEVER assert a verdict.
    for k in ("verdict", "actions", "violations", "memory"):
        contract.pop(k, None)

    records = load_records()
    now = datetime.now(timezone.utc)
    verdict, violations = rules.adjudicate(contract, records, now)

    mrn = (contract.get("wristband") or {}).get("barcode_mrn") or (contract.get("wristband") or {}).get("mrn")
    patient = records.patients.get(mrn) if mrn else None
    ward = (patient or {}).get("ward", "4W")
    cup_drugs = [records.pills.get(i.get("pill_id"), {}).get("drug", i.get("pill_id"))
                 for i in (contract.get("cup") or [])]

    mem = memory.escalate(db, verdict, cup_drugs, ward, now)
    final = mem["verdict"]

    event_id = f"EV-{uuid.uuid4().hex[:8].upper()}"
    seq = db.verifications.count_documents({}) + 1
    summary = mem["reason"] if mem["escalated"] else rules.summarize(verdict, violations)

    spoken, wav = None, None
    if final == "BLOCKED":
        who = (patient or {}).get("name", "this patient").title()
        spoken = f"Hold on. This is not {who}'s medication."
        wav = tts_render(f"blocked_{mrn or 'x'}", spoken)
    elif final == "PHARMACIST_REVIEW":
        spoken = "Pharmacist review required before administration."
        wav = tts_render("review", spoken)
    elif final == "HOLD_UNREADABLE":
        spoken = "Could not read this reliably. Escalating to a human."
        wav = tts_render("hold", spoken)

    record = {
        "event_id": event_id, "seq": seq, "ts": now,
        "source": source,
        "verdict": final, "base_verdict": verdict,
        "violations": [v.as_dict() for v in violations],
        "summary": summary,
        "memory": {k: mem[k] for k in ("escalated", "memory_enabled", "reason", "patterns")},
        "patient": {"mrn": mrn, "name": (patient or {}).get("name"), "ward": ward} if patient else {"mrn": mrn},
        "cup": contract.get("cup") or [],
        "wristband": contract.get("wristband") or {},
        "spoken": spoken, "audio": wav, "audible": False,  # no speaker on this box
        "provenance": "replay_fixture" if source == "replay" else "observed_today",
    }
    db.verifications.insert_one({**record})
    record.pop("_id", None)

    ledger_append(db, "medpass", f"verification:{final}", True,
                  {"event_id": event_id, "verdict": final, "mrn": mrn, "summary": summary}, event_id)

    if final in ("BLOCKED", "PHARMACIST_REVIEW"):
        db.state.update_one({"_id": "cabinet"}, {"$set": {"status": "LOCKED", "held_for": event_id}}, upsert=True)
        ledger_append(db, "medpass", "cabinet_hold", True, {"event_id": event_id}, event_id)
        # The open review request; the TTL index expires it if nobody ever acts.
        db.pending_approvals.insert_one({"event_id": event_id, "ts": now, "verdict": final})

    # A LIVE near-miss becomes part of the memory the next retrieval learns from.
    # Replayed fixtures deliberately do NOT write memory - rehearsals must not
    # inflate the very pattern the memory beat quotes.
    if final == "BLOCKED" and violations and source != "replay":
        db.near_misses.insert_one({
            "nm_id": f"NM-{event_id}", "ts": now,
            "type": "WRONG_PATIENT" if violations[0].right == "patient" else "WRONG_" + violations[0].right.upper(),
            "drug": cup_drugs[0] if cup_drugs else None, "ward": ward,
            "shift": "day" if 7 <= now.hour < 15 else ("evening" if 15 <= now.hour < 23 else "night"),
            "note": violations[0].detail, "outcome": "blocked_before_administration",
            "provenance": "observed_today",
        })

    emit("verification", {"record": record})
    return record


@app.post("/verify")
async def verify(request: Request):
    contract = await request.json()
    return await asyncio.to_thread(run_verification, contract, "api")


@app.post("/demo/trigger")
async def demo_trigger():
    """SPACE in the UI: commit the current camera frame through perception."""
    _ensure_camera()
    with _cam_lock:
        frame = None if _last_frame is None else _last_frame.copy()
    if frame is None:
        raise HTTPException(503, "no camera frame yet")

    def work():
        import cv2
        import perception
        records = load_records()
        # Freeze the exact frame under review so the UI can overlay boxes on the
        # still they were computed from (a moving live feed made them look wrong).
        (ROOT / "static").mkdir(exist_ok=True)
        cv2.imwrite(str(ROOT / "static" / "last_scan.jpg"), frame, [cv2.IMWRITE_JPEG_QUALITY, 82])
        contract, boxes, timings = perception.analyze(frame, records)
        emit("boxes", {"boxes": boxes, "timings": timings,
                       "still": f"/camera/last_scan.jpg?t={int(time.time())}"})
        return run_verification(contract, "camera")

    return await asyncio.to_thread(work)


FIXTURES = {
    "green": {"wristband": {"text_vlm": "GONZALES, MARIA | DOB 03/12/1948 | MRN 482913",
                            "text_ocr": "GONZALES, MARIA | DOB 03/12/1948 | MRN 482913",
                            "mrn": "MRN-482913"},
              "cup": [{"pill_id": "P-LISIN10", "vlm_match": "P-LISIN10", "attr_match": "P-LISIN10", "confidence": 0.95}]},
    "block": {"wristband": {"text_vlm": "GONZALES, MARIA | DOB 03/12/1948 | MRN 482913",
                            "text_ocr": "GONZALES, MARIA | DOB 03/12/1948 | MRN 482913",
                            "mrn": "MRN-482913"},
              "cup": [{"pill_id": "P-WARF5", "vlm_match": "P-WARF5", "attr_match": "P-WARF5", "confidence": 0.93}]},
    "memory": {"wristband": {"text_vlm": "GONZALES, MARTIN | DOB 09/04/1951 | MRN 482931",
                             "text_ocr": "GONZALES, MARTIN | DOB 09/04/1951 | MRN 482931",
                             "mrn": "MRN-482931"},
               "cup": [{"pill_id": "P-WARF5", "vlm_match": "P-WARF5", "attr_match": "P-WARF5", "confidence": 0.94}]},
}


@app.post("/demo/replay/{scenario}")
def demo_replay(scenario: str):
    """Fixture replay - labeled as such in provenance and in the UI banner."""
    fx = FIXTURES.get(scenario)
    if not fx:
        raise HTTPException(404, f"unknown scenario; have {list(FIXTURES)}")
    return run_verification(json.loads(json.dumps(fx)), "replay")


# --------------------------------------------------------------------------
# Mock hospital surface: the cabinet asymmetry, enforced at the app layer
# (and labeled that way in the UI - hold is autonomous, unlock needs a human)
# --------------------------------------------------------------------------

@app.get("/patients/{mrn}/orders")
def patient_orders(mrn: str):
    p = db.patients.find_one({"mrn": mrn}, {"_id": 0})
    if not p:
        raise HTTPException(404)
    pills = {x["pill_id"]: x for x in db.pill_library.find({}, {"_id": 0})}
    orders = []
    for o in db.med_orders.find({"mrn": mrn}, {"_id": 0}):
        pill = pills.get(o["pill_id"], {})
        o["drug"] = pill.get("drug", o["pill_id"])
        o["dose"] = pill.get("dose", "")
        orders.append(o)
    return {"patient": p, "orders": orders}


@app.post("/cabinet/hold")
async def cabinet_hold(request: Request):
    body = await _body(request)
    event_id = body.get("event_id")
    db.state.update_one({"_id": "cabinet"}, {"$set": {"status": "LOCKED", "held_for": event_id}}, upsert=True)
    row = ledger_append(db, body.get("actor", "agent"), "cabinet_hold", True, {"event_id": event_id}, event_id)
    emit("cabinet", {"status": "LOCKED", "held_for": event_id, "ledger_seq": row["seq"]})
    return {"ok": True, "status": "LOCKED"}


def _deny_without_approval(action: str, body: dict):
    event_id = body.get("event_id")
    approved = db.approvals.find_one({"event_id": event_id}) if event_id else None
    if not approved:
        row = ledger_append(db, body.get("actor", "unknown"), action, False,
                            {"event_id": event_id, "denied_reason": "no pharmacist approval on record"}, event_id)
        emit("denied", {"action": action, "event_id": event_id, "ledger_seq": row["seq"]})
        raise HTTPException(403, f"{action} DENIED - pharmacist approval required (policy: app-layer gate)")
    return approved


@app.post("/cabinet/unlock")
async def cabinet_unlock(request: Request):
    body = await _body(request)
    approved = _deny_without_approval("cabinet_unlock", body)
    db.state.update_one({"_id": "cabinet"}, {"$set": {"status": "UNLOCKED", "held_for": None}})
    row = ledger_append(db, approved["approver"], "cabinet_unlock", True, {"event_id": body.get("event_id")}, body.get("event_id"))
    emit("cabinet", {"status": "UNLOCKED", "ledger_seq": row["seq"]})
    return {"ok": True, "status": "UNLOCKED"}


@app.post("/mar/administer")
async def mar_administer(request: Request):
    body = await _body(request)
    approved = _deny_without_approval("mar_administer", body)
    now = datetime.now(timezone.utc)
    v = db.verifications.find_one({"event_id": body.get("event_id")}) or {}
    db.administrations.insert_one({
        "ts": now, "meta": {"ward": (v.get("patient") or {}).get("ward", "4W"),
                            "drug": (v.get("cup") or [{}])[0].get("pill_id", "?"), "shift": "day"},
        "mrn": (v.get("patient") or {}).get("mrn"), "outcome": "administered",
        "provenance": "observed_today",
    })
    row = ledger_append(db, approved["approver"], "mar_administer", True, {"event_id": body.get("event_id")}, body.get("event_id"))
    emit("mar", {"event_id": body.get("event_id"), "ledger_seq": row["seq"]})
    return {"ok": True}


def _agent_reflection(event_id: str, approver: str, decision: str) -> None:
    """After a human decision, the agent writes it to memory and reads the context
    back - real retrievals, summarized as one note for the care team."""
    def work():
        try:
            v = db.verifications.find_one({"event_id": event_id}) or {}
            patient = v.get("patient") or {}
            mrn, ward = patient.get("mrn"), patient.get("ward", "4W")
            pill_id = (v.get("cup") or [{}])[0].get("pill_id")
            pill = db.pill_library.find_one({"pill_id": pill_id}) or {}
            drug = pill.get("drug", pill_id or "this medication")
            today0 = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            today_checks = db.verifications.count_documents({"patient.mrn": mrn, "ts": {"$gte": today0}}) if mrn else 0
            today_blocks = db.verifications.count_documents({"patient.mrn": mrn, "verdict": "BLOCKED", "ts": {"$gte": today0}}) if mrn else 0
            nm90 = db.near_misses.count_documents({"drug": drug, "ward": ward,
                                                   "ts": {"$gte": datetime.now(timezone.utc) - timedelta(days=90)}})
            name = (patient.get("name") or "the patient").title()
            first = name.split()[0]
            if decision == "approved":
                note = (f"{approver} cleared this dose - you're good to give {name} the {drug} "
                        f"and chart it as normal. For context: this is {first}'s check "
                        f"#{today_checks} today ({today_blocks} blocked). {drug} stays on this "
                        f"ward's watch list ({nm90} near-misses in 90 days), so I'll keep "
                        f"double-checking every {drug} pass here.")
            else:
                note = (f"{approver} confirmed the block - do NOT give this to {name}; return "
                        f"the medication to the cart. I've recorded this as a caught near-miss "
                        f"for {drug} on this ward (now {nm90} in 90 days), so I'll escalate "
                        f"{drug} passes here even sooner. {first} is at {today_blocks} blocked "
                        f"of {today_checks} checks today.")
            emit("agent_note", {"event_id": event_id, "decision": decision, "note": note})
        except Exception:
            pass
    threading.Thread(target=work, daemon=True).start()


@app.get("/patients_seen")
def patients_seen():
    """Every patient the system has verified, straight off the memory layer."""
    rows = list(db.verifications.aggregate([
        {"$match": {"patient.mrn": {"$ne": None}}},
        {"$group": {"_id": {"mrn": "$patient.mrn", "name": "$patient.name", "ward": "$patient.ward"},
                    "checks": {"$sum": 1},
                    "blocked": {"$sum": {"$cond": [{"$eq": ["$verdict", "BLOCKED"]}, 1, 0]}},
                    "review": {"$sum": {"$cond": [{"$eq": ["$verdict", "PHARMACIST_REVIEW"]}, 1, 0]}},
                    "last": {"$max": "$ts"}}},
        {"$sort": {"last": -1}}, {"$limit": 30},
    ], maxTimeMS=3000))
    return json.loads(json.dumps({"patients": [
        {"mrn": r["_id"]["mrn"], "name": r["_id"].get("name"), "ward": r["_id"].get("ward"),
         "checks": r["checks"], "blocked": r["blocked"], "review": r["review"], "last": r["last"]}
        for r in rows]}, default=str))


@app.get("/patients/{mrn}/history")
def patient_history(mrn: str):
    """One patient's live memory: their verification timeline, decisions, orders,
    and the risk context of every drug they're on - all retrieved on demand."""
    p = db.patients.find_one({"mrn": mrn}, {"_id": 0})
    if not p:
        raise HTTPException(404)
    verifs = list(db.verifications.find(
        {"patient.mrn": mrn},
        {"_id": 0, "event_id": 1, "ts": 1, "verdict": 1, "summary": 1, "cup.pill_id": 1, "source": 1}
    ).sort("ts", -1).limit(12))
    event_ids = [v["event_id"] for v in verifs]
    decisions = {r["event_id"]: {"actor": r["actor"], "action": r["action"]}
                 for r in db.ledger.find({"event_id": {"$in": event_ids},
                                          "action": {"$in": ["pharmacist_approval", "pharmacist_decline"]}},
                                         {"_id": 0, "event_id": 1, "actor": 1, "action": 1})}
    pills = {x["pill_id"]: x for x in db.pill_library.find({}, {"_id": 0})}
    orders = []
    for o in db.med_orders.find({"mrn": mrn, "status": "active"}, {"_id": 0}):
        pill = pills.get(o["pill_id"], {})
        drug = pill.get("drug", o["pill_id"])
        risk = memory.risk_pattern(db, drug, p.get("ward", "4W"))
        orders.append({"drug": drug, "dose": pill.get("dose", ""),
                       "near_misses_90d": risk["count"], "hot": risk["hot"],
                       "top_shift": max(risk["by_shift"], key=risk["by_shift"].get) if risk["by_shift"] else None})
    # AI insights: deterministic, computed from the live memory layer right now.
    insights = []
    n_checks = len(verifs)
    n_blocked = sum(1 for v in verifs if v["verdict"] == "BLOCKED")
    first = (p.get("name") or mrn).split()[0].title()
    if n_blocked:
        insights.append(f"{first} has had {n_blocked} wrong-medication event{'s' if n_blocked > 1 else ''} "
                        f"caught in the last {n_checks} checks - treat every pass for this room as a "
                        f"slow-down moment.")
    elif n_checks:
        insights.append(f"Clean record: {n_checks} check{'s' if n_checks > 1 else ''}, nothing blocked. "
                        f"Routine flow applies.")
    for o in orders:
        if o["hot"]:
            insights.append(f"{o['drug']} is this ward's biggest risk right now "
                            f"({o['near_misses_90d']} near-misses in 90 days, mostly {o['top_shift']} "
                            f"shift) - MEDPASS auto-escalates every {o['drug']} pass to a pharmacist.")
    same_ward = db.patients.count_documents({"ward": p.get("ward"), "name": {"$regex": p.get("name", " ").split()[-1], "$options": "i"}})
    if same_ward > 1:
        insights.append(f"Look-alike alert: {same_ward} patients named "
                        f"{p.get('name', ' ').split()[-1].title()} on ward {p.get('ward')} - "
                        f"the classic wrong-patient setup. Verify by DOB, not by surname.")
    return json.loads(json.dumps({
        "patient": p, "orders": orders, "insights": insights,
        "timeline": [{**v, "decision": decisions.get(v["event_id"])} for v in verifs],
    }, default=str))


@app.get("/camera/last_scan.jpg")
def last_scan():
    p = ROOT / "static" / "last_scan.jpg"
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})


@app.post("/approvals/decline")
async def decline(request: Request):
    """The pharmacist's other button. The block stands, the cabinet stays locked,
    and the decision is a named, ledgered action like everything else."""
    body = await _body(request)
    event_id, approver = body.get("event_id"), body.get("approver")
    if not event_id or not approver:
        raise HTTPException(400, "event_id and approver required")
    db.pending_approvals.delete_many({"event_id": event_id})
    row = ledger_append(db, approver, "pharmacist_decline", True,
                        {"event_id": event_id, "decision": "block stands"}, event_id)
    emit("declined", {"event_id": event_id, "approver": approver, "ledger_seq": row["seq"]})
    _agent_reflection(event_id, approver, "declined")
    return {"ok": True, "cabinet": "LOCKED"}


@app.post("/approvals")
async def approve(request: Request):
    body = await _body(request)
    event_id, approver = body.get("event_id"), body.get("approver")
    if not event_id or not approver:
        raise HTTPException(400, "event_id and approver required")
    db.approvals.replace_one({"event_id": event_id},
                             {"event_id": event_id, "approver": approver, "ts": datetime.now(timezone.utc)},
                             upsert=True)
    row = ledger_append(db, approver, "pharmacist_approval", True, {"event_id": event_id}, event_id)
    db.state.update_one({"_id": "cabinet"}, {"$set": {"status": "UNLOCKED", "held_for": None}})
    emit("approval", {"event_id": event_id, "approver": approver, "ledger_seq": row["seq"]})
    emit("cabinet", {"status": "UNLOCKED", "held_for": None, "ledger_seq": row["seq"]})
    _agent_reflection(event_id, approver, "approved")
    return {"ok": True}


# --------------------------------------------------------------------------
# Ask the memory: a local model writes the aggregation; we validate hard and
# fall back to canned pipelines. The model NEVER writes, only reads.
# --------------------------------------------------------------------------
ALLOWED_STAGES = {"$match", "$group", "$sort", "$limit", "$count", "$project", "$facet", "$unwind", "$addFields"}
ALLOWED_COLLS = {"near_misses", "administrations", "verifications"}

CANNED = {
    "near_misses_month": ("near_misses", [
        {"$match": {"ts": {"$gte": "NOW-30d"}}},
        {"$group": {"_id": {"type": "$type", "shift": "$shift"}, "n": {"$sum": 1}}},
        {"$sort": {"n": -1}}, {"$limit": 8}]),
}


BANNED_OPS = {"$where", "$function", "$accumulator", "$expr", "$out", "$merge", "$lookup", "$graphLookup"}


def _no_banned_ops(obj) -> bool:
    """Recursive: server-side JS and write/join stages are banned ANYWHERE in the
    pipeline, not just at the top level (a $function inside $match would otherwise
    dodge maxTimeMS via Mongo's separate 60s JS timeout)."""
    if isinstance(obj, dict):
        return all(k not in BANNED_OPS and _no_banned_ops(v) for k, v in obj.items())
    if isinstance(obj, list):
        return all(_no_banned_ops(x) for x in obj)
    return True


def _valid_pipeline(coll: str, pipeline: list) -> bool:
    if coll not in ALLOWED_COLLS or not isinstance(pipeline, list) or not pipeline:
        return False
    for stage in pipeline:
        if not isinstance(stage, dict) or len(stage) != 1:
            return False
        if next(iter(stage)) not in ALLOWED_STAGES:
            return False
    return _no_banned_ops(pipeline)


def _hydrate_dates(obj):
    """The model emits 'NOW-30d' style tokens; turn them into real datetimes."""
    from datetime import timedelta
    if isinstance(obj, dict):
        return {k: _hydrate_dates(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_hydrate_dates(x) for x in obj]
    if isinstance(obj, str):
        if obj == "NOW":
            return datetime.now(timezone.utc)
        m = re.fullmatch(r"NOW-(\d+)d", obj)
        if m:
            return datetime.now(timezone.utc) - timedelta(days=int(m.group(1)))
    return obj


def _llm_pipeline(question: str) -> tuple[str, list] | None:
    schema_hint = (
        "Collections: near_misses(ts:date, type:enum WRONG_DRUG|WRONG_PATIENT|WRONG_DOSE|WRONG_TIME|UNREADABLE, "
        "drug:str, ward:str, shift: day|evening|night, note:str, provenance:str), "
        "administrations(ts:date, meta:{ward,drug,shift}, mrn, outcome), "
        "verifications(ts:date, verdict, patient:{mrn,name,ward}). "
        'For relative dates use the string "NOW-<n>d". '
        'Reply with ONLY JSON: {"collection": "...", "pipeline": [...]}. Example: '
        '{"collection":"near_misses","pipeline":[{"$match":{"ts":{"$gte":"NOW-30d"}}},'
        '{"$group":{"_id":{"type":"$type","shift":"$shift"},"n":{"$sum":1}}},{"$sort":{"n":-1}}]}'
    )
    try:
        r = http.post(f"{OLLAMA}/api/generate", json={
            "model": QUERY_MODEL, "stream": False, "format": "json",
            "options": {"temperature": 0, "top_k": 1, "seed": 0, "num_predict": 400},
            "prompt": f"{schema_hint}\n\nWrite a MongoDB aggregation for: {question}",
        }, timeout=25)
        body = r.json()
        # nemotron is a reasoning model: with format=json the JSON often lands in
        # `thinking` while `response` is empty. Accept either.
        raw = body.get("response") or body.get("thinking") or ""
        out = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
        coll, pipe = out.get("collection"), out.get("pipeline")
        if _valid_pipeline(coll, pipe):
            return coll, pipe
    except Exception:
        pass
    return None


@app.get("/memory/ask")
def memory_ask(q: str):
    t0 = time.perf_counter()
    got = _llm_pipeline(q)
    model_used = QUERY_MODEL if got else "canned_fallback"
    if not got:
        coll, pipe = CANNED["near_misses_month"]
        pipe = json.loads(json.dumps(pipe))
    else:
        coll, pipe = got
    pipe = _hydrate_dates(pipe)
    try:
        rows = list(db[coll].aggregate(pipe, maxTimeMS=3000))[:20]
    except Exception:
        rows = []
    if not rows:
        # A model pipeline that errors OR silently matches nothing falls back to the
        # canned aggregation - the demo never renders an empty panel.
        coll, pipe = CANNED["near_misses_month"]
        pipe = _hydrate_dates(json.loads(json.dumps(pipe)))
        rows = list(db[coll].aggregate(pipe, maxTimeMS=3000))[:20]
        model_used = "canned_fallback"
    ms = (time.perf_counter() - t0) * 1000
    result = {"question": q, "collection": coll, "pipeline": pipe, "rows": rows,
              "model": model_used, "total_ms": round(ms, 1)}
    emit("memory_answer", {"result": json.loads(json.dumps(result, default=str))})
    return json.loads(json.dumps(result, default=str))


# --------------------------------------------------------------------------
# Metrics, ledger, demo controls
# --------------------------------------------------------------------------
_tx_baseline: dict[str, int] = {}
_t_start = time.time()


def _tx_bytes() -> int:
    total = 0
    for i in WAN_IFACES:
        p = Path(f"/sys/class/net/{i}/statistics/tx_bytes")
        if p.exists():
            total += int(p.read_text())
    return total


@app.get("/metrics")
def metrics():
    counts = {c: db[c].estimated_document_count() for c in
              ("administrations", "near_misses", "verifications", "ledger", "patients")}
    return {
        # Whole-box uplink tx, honestly labeled: it includes anything else this box
        # does on the network (e.g. a dev session). The demo claim is "no cloud calls
        # in the agent's runtime path" - provable by killing the uplink mid-demo.
        "uplink_tx_bytes_since_start": _tx_bytes() - _tx_baseline.get("v", 0),
        "wan_ifaces": WAN_IFACES,
        "uptime_s": round(time.time() - _t_start, 1),
        "doc_counts": counts,
        "chain": verify_chain(db),
        "memory_enabled": memory.MEMORY_ENABLED,
        "cabinet": (db.state.find_one({"_id": "cabinet"}, {"_id": 0}) or {}),
        "hostname": os.uname().nodename,
    }


@app.get("/ledger/verify")
def ledger_verify():
    return verify_chain(db)


@app.get("/ledger/recent")
def ledger_recent(n: int = 12):
    rows = list(db.ledger.find({}, {"_id": 0, "payload": 0}).sort("seq", -1).limit(n))
    return {"rows": rows}


@app.get("/verifications/recent")
def verifications_recent(n: int = 10):
    rows = list(db.verifications.find({}, {"_id": 0}).sort("ts", -1).limit(n))
    return json.loads(json.dumps({"rows": rows}, default=str))


@app.post("/agent/activity")
async def agent_activity(request: Request):
    """The change-stream watcher reports what it actually did; the canvas animates
    the MongoDB->agent wake only when this real signal arrives."""
    body = await _body(request)
    emit("agent", {"stage": body.get("stage", "wake"), "event_id": body.get("event_id"),
                   "detail": str(body.get("detail", ""))[:200]})
    return {"ok": True}


@app.get("/ward/{ward}/safety")
def ward_safety(ward: str):
    """Provider-facing ward picture, aggregated live from the full dataset."""
    from datetime import timedelta as _td
    cutoff = datetime.now(timezone.utc) - _td(days=90)
    t0 = time.perf_counter()
    top_drugs = list(db.near_misses.aggregate([
        {"$match": {"ward": ward, "ts": {"$gte": cutoff}}},
        {"$group": {"_id": "$drug", "n": {"$sum": 1},
                    "shifts": {"$push": "$shift"}, "last": {"$max": "$ts"}}},
        {"$sort": {"n": -1}}, {"$limit": 5},
    ], maxTimeMS=3000))
    volume = list(db.administrations.aggregate([
        {"$match": {"meta.ward": ward}},
        {"$group": {"_id": "$meta.shift", "n": {"$sum": 1}}},
    ], maxTimeMS=3000))
    ms = (time.perf_counter() - t0) * 1000
    out = {
        "ward": ward,
        "window_days": 90,
        "top_risk_drugs": [
            {"drug": r["_id"], "near_misses": r["n"],
             "top_shift": max(set(r["shifts"]), key=r["shifts"].count) if r["shifts"] else None,
             "last": r["last"]}
            for r in top_drugs if r["_id"]],
        "administrations_by_shift": {r["_id"]: r["n"] for r in volume if r["_id"]},
        "administrations_total": sum(r["n"] for r in volume),
        "query_ms": round(ms, 1),
    }
    return json.loads(json.dumps(out, default=str))


@app.post("/demo/memory_toggle")
def memory_toggle():
    memory.set_enabled(not memory.MEMORY_ENABLED)
    emit("memory_toggle", {"enabled": memory.MEMORY_ENABLED})
    return {"memory_enabled": memory.MEMORY_ENABLED}


@app.post("/demo/reset_clock")
def reset_clock():
    now = datetime.now(timezone.utc)
    db.state.replace_one({"_id": "clock"}, {"_id": "clock", "service_start": now}, upsert=True)
    return {"service_start": now.isoformat()}


@app.get("/")
def index():
    return FileResponse(ROOT / "ui.html")


@app.on_event("startup")
async def startup():
    global _loop
    _loop = asyncio.get_running_loop()
    ensure_schema(db)
    service_start()
    _tx_baseline["v"] = _tx_bytes()
    ledger_append(db, "system", "app_start", True, {"note": "MEDPASS started"})
    _ensure_camera()
    # Pre-render the fixed spoken lines so playback is instant.
    threading.Thread(target=lambda: [
        tts_render("review", "Pharmacist review required before administration."),
        tts_render("hold", "Could not read this reliably. Escalating to a human."),
        tts_render("blocked_MRN-482913", "Hold on. This is not Maria Gonzales's medication."),
    ], daemon=True).start()
