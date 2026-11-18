# MEDPASS — where MongoDB does the work

Three things MongoDB does in MEDPASS, and where to see each one in the running system.
All data is synthetic.

## 1. The agent survives its own sandbox

**Design:** kill any process and nothing is lost, because MongoDB (Community 8, Docker
named volume `medpass_mongo`) is the *only* runtime source of truth. There are no
in-process caches of record; the JSON files in `data/` are seed input only.

**Where to see it:**
- Ctrl-C the API mid-demo, restart it. Every verification, approval, cabinet state,
  ledger row and learned near-miss returns. The hash chain re-verifies over the same
  rows (`GET /ledger/verify`).
- The service clock itself is persisted (`state` collection), so even the demo's
  dose-window schedule survives a restart.
- `db.py` — no state outside Mongo; `api.py` reloads everything per request.

## 2. Retrieval that changes behavior

**Design:** MongoDB is not a log on the side — its answer *changes the verdict*.

**Mechanism** (`memory.py`): after the deterministic rules pass a cup, an aggregation
(`$match` on drug + ward + 90-day window over `near_misses`, `$facet` for counts,
shift breakdown, and recent evidence) runs at verify time. If the count clears the
threshold, VERIFIED escalates to PHARMACIST_REVIEW and the UI shows the retrieved
evidence, the pipeline, the index used (`risk_lookup`), docs examined, and the query
latency measured on the running system.

**Where to see it:**
- Martin's wristband + Martin's **own** warfarin — correct on every rule — is
  escalated because the database remembers warfarin near-misses on this ward.
  Press M (MEMORY OFF), same cup: VERIFIED. That contrast *is* retrieval changing
  behavior, live and deterministic.
- The loop closes: every live BLOCK inserts an `observed_today` near-miss, which the
  very next retrieval counts. The system gets more cautious as incidents accrue.
- Safety envelope: escalation is deterministic DB retrieval (never model output) and
  can only ever *add* human review — unit-tested in `test_memory.py` (never-downgrade,
  window math, toggle-is-a-true-noop).
- `GET /memory/ask` — a local NVIDIA model writes the aggregation from plain
  English, validated against a stage/collection whitelist, executed with `maxTimeMS`.

## 3. Business-shaped data, not a fixture

- `administrations` is a **native time-series collection** (timeField `ts`, metaField
  `meta`, granularity hours) holding **49,950 synthetic events across 12 months**, 200
  patients, 40 staff, 3 wards, 25 drugs.
- `near_misses` carries the derived risk history with the two seeded clusters a safety
  officer would recognize: metoprolol/metformin look-alike confusions on night shift,
  and the warfarin wrong-patient cluster on ward 4W.
- **Iterable, not static:** live verifications, approvals, ledger rows and
  observed-today near-misses append continuously; provenance labels (`seeded_synthetic`
  vs `observed_today`) keep the synthetic data visible as such in the UI.
- MongoDB features doing real work: single-node **replica set** enabling **change
  streams** (the agent's wake signal — zero polling) and **transactions** (hash-chain
  ledger appends), `$jsonSchema` validators on the safety-critical collections, a TTL
  index expiring stale pending approvals, and aggregation `explain()` surfaced in the
  product UI.

## Where OpenClaw sits

OpenClaw's gateway runs with hooks enabled; the MongoDB change stream wakes
`agent_watch.py`, which places the cabinet hold over HTTP and wakes the OpenClaw agent
(`POST /hooks/wake`, Bearer) with the incident. Standing orders live in
`config/AGENTS.md` in OpenClaw's Program / Authority / Trigger / Approval-gate /
Escalation grammar — including the product's core asymmetry: it may block an error by
itself; it may never approve one.
