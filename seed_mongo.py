"""Seed MEDPASS MongoDB with 12 months of synthetic hospital operations data.

This is deliberately NOT a toy: ~50k administration events in a native time-series
collection, 200 patients, 40 staff, 3 wards, ~25 drugs, with near-misses derived
from the event stream. A JSON file would not do it. Every seeded row is labeled
provenance=seeded_synthetic; anything observed live today is labeled observed_today.

Deterministic (seed 42) so every rehearsal reproduces the same world.

Run: .venv/bin/python seed_mongo.py
"""
from __future__ import annotations

import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from db import ensure_schema, get_db

RNG = random.Random(42)
NOW = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
DAYS = 365
WARDS = ["3E", "4W", "5N"]  # Maria & Martin live on 4W
DEMO_WARD = "4W"

FIRST = ["JAMES", "MARY", "ROBERT", "PATRICIA", "JOHN", "JENNIFER", "MICHAEL", "LINDA", "DAVID", "ELIZABETH",
         "WILLIAM", "BARBARA", "RICHARD", "SUSAN", "JOSEPH", "JESSICA", "THOMAS", "SARAH", "CHARLES", "KAREN",
         "CHRISTOPHER", "LISA", "DANIEL", "NANCY", "MATTHEW", "BETTY", "ANTHONY", "MARGARET", "MARK", "SANDRA"]
LAST = ["SMITH", "JOHNSON", "WILLIAMS", "BROWN", "JONES", "GARCIA", "MILLER", "DAVIS", "RODRIGUEZ", "MARTINEZ",
        "HERNANDEZ", "LOPEZ", "WILSON", "ANDERSON", "THOMAS", "TAYLOR", "MOORE", "JACKSON", "MARTIN", "LEE",
        "PEREZ", "THOMPSON", "WHITE", "HARRIS", "SANCHEZ", "CLARK", "RAMIREZ", "LEWIS", "ROBINSON", "WALKER"]

# ~25 drugs; the look-alike pairs that fuel the near-miss story are real LASA pairs.
DRUGS = [
    ("P-LISIN10", "Lisinopril", "10 mg"), ("P-WARF5", "Warfarin", "5 mg"),
    ("P-METOP25", "Metoprolol", "25 mg"), ("P-METF500", "Metformin", "500 mg"),
    ("P-AMLO5", "Amlodipine", "5 mg"), ("P-ATOR20", "Atorvastatin", "20 mg"),
    ("P-OMEP20", "Omeprazole", "20 mg"), ("P-LEVO50", "Levothyroxine", "50 mcg"),
    ("P-GABA300", "Gabapentin", "300 mg"), ("P-HYDRO25", "Hydrochlorothiazide", "25 mg"),
    ("P-SERT50", "Sertraline", "50 mg"), ("P-FURO40", "Furosemide", "40 mg"),
    ("P-PANT40", "Pantoprazole", "40 mg"), ("P-PRED10", "Prednisone", "10 mg"),
    ("P-TRAM50", "Tramadol", "50 mg"), ("P-CLOP75", "Clopidogrel", "75 mg"),
    ("P-CELE200", "Celecoxib", "200 mg"), ("P-DULOX30", "Duloxetine", "30 mg"),
    ("P-ESCIT10", "Escitalopram", "10 mg"), ("P-ROSU10", "Rosuvastatin", "10 mg"),
    ("P-LOSAR50", "Losartan", "50 mg"), ("P-CARVE12", "Carvedilol", "12.5 mg"),
    ("P-APIX5", "Apixaban", "5 mg"), ("P-INSUL10", "Insulin glargine", "10 u"),
    ("P-DIGOX125", "Digoxin", "125 mcg"),
]
DRUG_BY_ID = {d[0]: d[1] for d in DRUGS}

SHIFT_OF = lambda h: "day" if 7 <= h < 15 else ("evening" if 15 <= h < 23 else "night")


def gen_staff():
    roles = ["nurse"] * 32 + ["charge_nurse"] * 5 + ["pharmacist"] * 3
    return [
        {"staff_id": f"S-{100 + i}", "name": f"{RNG.choice(FIRST).title()} {RNG.choice(LAST).title()}",
         "role": roles[i], "provenance": "seeded_synthetic"}
        for i in range(40)
    ]


def gen_patients():
    pts = []
    used = set()
    for i in range(200):
        while True:
            name = f"{RNG.choice(FIRST)} {RNG.choice(LAST)}"
            if name not in used:
                used.add(name)
                break
        mrn = f"MRN-{RNG.randint(100000, 999999)}"
        ward = RNG.choice(WARDS)
        pts.append({
            "mrn": mrn, "name": name, "ward": ward,
            "room": f"{ward}-{RNG.randint(1, 40):02d}",
            "dob": f"{RNG.randint(1935, 1990)}-{RNG.randint(1, 12):02d}-{RNG.randint(1, 28):02d}",
            "provenance": "seeded_synthetic",
        })
    return pts


def gen_administrations(patients, staff):
    """~50k events across 12 months. Nightly density mirrors a real MAR."""
    events = []
    nurses = [s for s in staff if s["role"] in ("nurse", "charge_nurse")]
    for day in range(DAYS):
        base = NOW - timedelta(days=DAYS - day)
        n_today = RNG.randint(120, 155)  # ~137/day average -> ~50k/year
        for _ in range(n_today):
            p = RNG.choice(patients)
            pill_id, drug, dose = RNG.choice(DRUGS)
            ts = base.replace(hour=RNG.choice([8, 9, 12, 13, 17, 18, 21, 22, 2]), minute=RNG.randint(0, 59))
            events.append({
                "ts": ts,
                "meta": {"ward": p["ward"], "drug": drug, "shift": SHIFT_OF(ts.hour)},
                "mrn": p["mrn"], "pill_id": pill_id, "dose": dose,
                "staff_id": RNG.choice(nurses)["staff_id"],
                "outcome": "administered",
                "provenance": "seeded_synthetic",
            })
    return events


def gen_near_misses(patients, staff):
    """Derived from the event stream at a realistic ~0.6% rate, with two deliberate
    clusters that the memory layer will learn from:
      - metoprolol/metformin LASA confusions, mostly night shift (the classic)
      - warfarin wrong-patient events on ward 4W in the last 90 days  <- THE BEAT
    """
    nms = []
    nurses = [s for s in staff if s["role"] in ("nurse", "charge_nurse")]
    nm_seq = 1000

    def add(ts, typ, drug, ward, note, shift=None):
        nonlocal nm_seq
        nm_seq += 1
        nms.append({
            "nm_id": f"NM-{nm_seq}", "ts": ts, "type": typ, "drug": drug, "ward": ward,
            "shift": shift or SHIFT_OF(ts.hour), "staff_id": RNG.choice(nurses)["staff_id"],
            "note": note, "outcome": "blocked_before_administration",
            "provenance": "seeded_synthetic",
        })

    # Background noise: ~250 assorted near-misses over the year.
    for _ in range(250):
        ts = NOW - timedelta(days=RNG.uniform(0, DAYS), hours=RNG.uniform(0, 24))
        pill_id, drug, _ = RNG.choice(DRUGS)
        typ = RNG.choice(["WRONG_DOSE", "WRONG_TIME", "WRONG_DRUG", "WRONG_PATIENT", "UNREADABLE"])
        add(ts, typ, drug, RNG.choice(WARDS), RNG.choice([
            "double tablet in cup", "dose outside window", "look-alike packaging",
            "wristband mismatch caught at bedside", "faded wristband, unreadable"]))

    # Cluster 1: metoprolol/metformin, night shift, all year (~28 events).
    for _ in range(28):
        ts = (NOW - timedelta(days=RNG.uniform(0, DAYS))).replace(hour=RNG.choice([1, 2, 3, 23]))
        add(ts, "WRONG_DRUG", RNG.choice(["Metoprolol", "Metformin"]), RNG.choice(WARDS),
            "look-alike name confusion metoprolol/metformin")

    # Cluster 2 - the demo beat: 9 warfarin wrong-patient near-misses on 4W, last 90 days.
    for i in range(9):
        ts = NOW - timedelta(days=RNG.uniform(2, 88))
        if i < 7:  # mostly night shift, for the pattern sentence
            ts = ts.replace(hour=RNG.choice([1, 2, 3, 22, 23]))
        add(ts, "WRONG_PATIENT", "Warfarin", DEMO_WARD,
            "warfarin drawn for wrong patient - adjacent rooms, similar names")

    return nms


def load_demo_entities(db):
    """The two demo patients + orders + pill library come from the JSON seed files."""
    d = Path(__file__).parent / "data"
    patients = json.loads((d / "patients.json").read_text())
    for p in patients:
        p["ward"] = DEMO_WARD
        p["provenance"] = "seeded_synthetic"
        db.patients.replace_one({"mrn": p["mrn"]}, p, upsert=True)
    for o in json.loads((d / "med_orders.json").read_text()):
        o["count"] = int(o.get("count", 1))
        o["provenance"] = "seeded_synthetic"
        db.med_orders.replace_one({"order_id": o["order_id"]}, o, upsert=True)
    for pill in json.loads((d / "pill_library.json").read_text()):
        pill["provenance"] = "seeded_synthetic"
        db.pill_library.replace_one({"pill_id": pill["pill_id"]}, pill, upsert=True)


def main():
    db = get_db()
    print("dropping + reseeding", db.name)
    for c in ("administrations", "near_misses", "patients", "med_orders", "pill_library",
              "staff", "verifications", "ledger", "approvals", "pending_approvals", "state"):
        db.drop_collection(c)
    ensure_schema(db)

    staff = gen_staff()
    patients = gen_patients()
    db.staff.insert_many(staff)
    db.patients.insert_many(patients)

    admins = gen_administrations(patients, staff)
    for i in range(0, len(admins), 5000):
        db.administrations.insert_many(admins[i:i + 5000])
    nms = gen_near_misses(patients, staff)
    db.near_misses.insert_many(nms)

    load_demo_entities(db)
    db.state.replace_one({"_id": "cabinet"}, {"_id": "cabinet", "status": "LOCKED", "held_for": None}, upsert=True)

    print(f"administrations: {db.administrations.count_documents({})}")
    print(f"near_misses:     {db.near_misses.count_documents({})}")
    print(f"patients:        {db.patients.count_documents({})}")
    print(f"staff:           {db.staff.count_documents({})}")
    warf = db.near_misses.count_documents({"drug": "Warfarin", "ward": DEMO_WARD,
                                           "ts": {"$gte": NOW - timedelta(days=90)}})
    print(f"warfarin 4W near-misses in 90d (the beat): {warf}")


if __name__ == "__main__":
    sys.exit(main())
