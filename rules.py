"""MEDPASS decision layer.

No model output ever sets a verdict. Models perceive; these rules decide; a human approves.
That is the whole validation argument, and it is why this file is pure, deterministic and tested.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

VERIFIED = "VERIFIED"
BLOCKED = "BLOCKED"
PHARMACIST_REVIEW = "PHARMACIST_REVIEW"
HOLD_UNREADABLE = "HOLD_UNREADABLE"

CONFIDENCE_FLOOR = 0.80


@dataclass
class Violation:
    right: str          # patient | drug | dose | route | time | readability
    detail: str

    def as_dict(self):
        return {"right": self.right, "detail": self.detail}


@dataclass
class Records:
    patients: dict
    orders: list
    pills: dict
    service_start: datetime

    @classmethod
    def load(cls, data_dir: str | Path, service_start: datetime):
        d = Path(data_dir)
        patients = {p["mrn"]: p for p in json.loads((d / "patients.json").read_text())}
        orders = json.loads((d / "med_orders.json").read_text())
        pills = {p["pill_id"]: p for p in json.loads((d / "pill_library.json").read_text())}
        return cls(patients=patients, orders=orders, pills=pills, service_start=service_start)

    def active_orders(self, mrn: str):
        return [o for o in self.orders if o["mrn"] == mrn and o.get("status") == "active"]


def due_at(order: dict, service_start: datetime) -> datetime:
    """Demo scheduling is relative to service start, not wall clock.

    A wall-clock schedule (09:00/18:00/21:00) makes the green path impossible to show at
    an arbitrary demo hour, so orders carry demo_offset_min and we resolve it here.
    Always timezone-aware; naive input is treated as UTC rather than guessed at.
    """
    if service_start.tzinfo is None:
        service_start = service_start.replace(tzinfo=timezone.utc)
    return service_start + timedelta(minutes=int(order.get("demo_offset_min", 0)))


def in_window(order: dict, service_start: datetime, now: datetime) -> bool:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    window = int(order.get("window_min", 60))
    delta = abs((now - due_at(order, service_start)).total_seconds()) / 60.0
    return delta <= window


def _mrn_from(wristband: dict) -> str | None:
    """Barcode wins when present (deterministic channel), else the consensus text read."""
    return wristband.get("barcode_mrn") or wristband.get("mrn")


def read_is_conflicted(wristband: dict) -> bool:
    """A missing read is NOT agreement.

    Two empty strings must never be treated as two readers agreeing - that path would
    turn an unreadable band into a confident verdict.
    """
    if wristband.get("read_conflict"):
        return True
    vlm = (wristband.get("text_vlm") or "").strip() or None
    ocr = (wristband.get("text_ocr") or "").strip() or None
    if vlm is None or ocr is None:
        return True
    if _norm(vlm) != _norm(ocr):
        return True
    barcode = wristband.get("barcode_mrn")
    if barcode and wristband.get("mrn") and barcode != wristband["mrn"]:
        return True
    return False


def _norm(s: str) -> str:
    return "".join(ch for ch in s.upper() if ch.isalnum())


def adjudicate(contract: dict, records: Records, now: datetime | None = None) -> tuple[str, list[Violation]]:
    """Return (verdict, violations). Pure function - same inputs, same output, always."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    wristband = contract.get("wristband") or {}
    cup = contract.get("cup") or []
    v: list[Violation] = []

    # 1. Readability gate - never guess an identity.
    if read_is_conflicted(wristband):
        return HOLD_UNREADABLE, [Violation("readability", "readers disagreed or a read was empty; no verdict issued")]

    mrn = _mrn_from(wristband)
    patient = records.patients.get(mrn)
    if patient is None:
        return BLOCKED, [Violation("patient", f"wristband {mrn} matches no patient on this unit")]

    # 2. Empty cup is not a pass.
    if not cup:
        return HOLD_UNREADABLE, [Violation("readability", "no medication detected in the cup")]

    orders = records.active_orders(mrn)
    counts: dict[str, int] = {}
    for item in cup:
        counts[item.get("pill_id")] = counts.get(item.get("pill_id"), 0) + 1

    # 3. Every pill must trace to an active order for THIS patient.
    for pill_id, count in counts.items():
        pill = records.pills.get(pill_id)
        label = (pill or {}).get("drug", pill_id)
        matching = [o for o in orders if o["pill_id"] == pill_id]

        if not matching:
            # Is it ordered for somebody else? That is the wrong-patient story, and it is
            # the single most useful sentence we can put in front of a nurse.
            other = next((o for o in records.orders if o["pill_id"] == pill_id and o["mrn"] != mrn), None)
            if other:
                who = records.patients.get(other["mrn"], {}).get("name", other["mrn"])
                v.append(Violation("patient", f"{label} is ordered for {who}, not for {patient['name']}"))
            else:
                v.append(Violation("drug", f"{label} is not on {patient['name']}'s medication record"))
            continue

        order = matching[0]
        if not in_window(order, records.service_start, now):
            due = due_at(order, records.service_start)
            mins = int((now - due).total_seconds() // 60)
            when = f"due {abs(mins)} min ago" if mins > 0 else f"not due for {abs(mins)} min"
            v.append(Violation("time", f"{label} is outside its administration window ({when})"))

        expected = int(order.get("count", 1))
        if count != expected:
            v.append(Violation("dose", f"{label}: {count} in cup, {expected} ordered"))

    if v:
        return BLOCKED, v

    # 4. Perception uncertainty escalates to a human - it never silently passes.
    weak = [i for i in cup if float(i.get("confidence", 0)) < CONFIDENCE_FLOOR]
    if weak:
        worst = min(float(i.get("confidence", 0)) for i in weak)
        return PHARMACIST_REVIEW, [Violation("drug", f"identification confidence {worst:.2f} below {CONFIDENCE_FLOOR:.2f}")]

    return VERIFIED, []


def summarize(verdict: str, violations: list[Violation]) -> str:
    """One line for the UI card and the spoken warning."""
    if verdict == VERIFIED:
        return "Verified against the medication record."
    if verdict == HOLD_UNREADABLE:
        return "Could not read this reliably - escalated for a human check."
    if verdict == PHARMACIST_REVIEW:
        return "Needs pharmacist review before administration."
    return violations[0].detail if violations else "Blocked."
