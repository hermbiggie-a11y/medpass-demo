# MEDPASS — technical write-up

How MEDPASS is built, why each technology is there, and how MongoDB carries the
system's memory. Demo only: all patients and data are synthetic; this is not a
medical device.

---

## 1. System architecture

```
 camera (EMEET C960, /dev/video0)
    │  SPACE in the UI → POST /demo/trigger
    ▼
 perception.py ── Qwen3-VL locates wristband + pills, reads label   (proposes)
              ── Tesseract OCR re-reads the label crop              (verifies)
              ── OpenCV hue-presence vote on each pill crop         (verifies)
    │  emits a contract JSON — never a verdict
    ▼
 rules.py  ── pure, deterministic checks: patient / drug / dose / time / readability
    │  verdict: VERIFIED · BLOCKED · PHARMACIST_REVIEW · HOLD_UNREADABLE
    ▼
 memory.py ── MongoDB aggregation over near-miss history at verify time
    │  may escalate VERIFIED → PHARMACIST_REVIEW (never the reverse)
    ▼
 api.py    ── persists the verification, appends to the hash-chain ledger (txn),
    │         holds the cabinet on a block, renders TTS, emits SSE to the UI
    ▼
 MongoDB   ── the ONLY runtime state in the entire system
    │
    ├─ change stream ──► agent_watch.py ──► POST /cabinet/hold  +  OpenClaw /hooks/wake
    └─ SSE /events  ──► ui.html (verdict card, agent strip, knowledge graph, ledger rail)
```

The safety spine, which every design choice serves: **models perceive, rules decide,
memory escalates, humans approve.** No model output ever sets a verdict. Memory can
only ever *add* human review. Approval requires a named human.

---

## 2. Module by module

### `rules.py` + `test_rules.py` — the decision core
A pure function: `adjudicate(contract, records, now) -> (verdict, violations)`.
Checks: the wristband dual-read agreed (readability); the patient exists; every pill
in the cup traces to an *active order for this patient*; the dose count matches; the
time is inside the order's window; perception confidence clears a floor (below it →
pharmacist review, never a silent pass). 16 tests cover every branch, including the
subtle one: two *empty* reads must never count as two readers agreeing.

**Why deterministic Python and not a model?** Because "why did it block?" must have an
exact, reproducible answer. That's what makes a safety system auditable.

### `perception.py` — three independent readers
- **Qwen3-VL-30B** (Ollama, temperature 0, seed 0) returns bounding boxes on its
  native 0–1000 grid plus the label text it can read and each pill's color/shape.
- **Tesseract OCR** re-reads the full-resolution wristband crop (deskewed, upscaled,
  three preprocessing variants). Both readers must name the **same patient** —
  matching on first names (MARIA vs MARTIN), the distinguishing token in the
  deliberate look-alike-surname trap.
- **OpenCV** classifies each pill crop by **hue-presence voting**: the fraction of
  saturated pixels in each calibrated hue band. This beats a median because a pill
  box often includes the amber bottle — a median says "orange bottle" even when a
  third of the pixels are clearly green capsules. Bands were calibrated on the actual
  props from live frames, so they need recalibrating for other props or lighting.
- VLM and OpenCV disagreeing about a pill caps confidence at 0.5 → the rules escalate.
  Any failure anywhere degrades to an honest HOLD, never a crash and never a guess.

### `db.py` — the MongoDB layer
Connection, schema, and the ledger. Details in section 4.

### `seed_mongo.py` — the synthetic year
Deterministic (seed 42): 200 patients, 40 staff, 3 wards, 25 drugs, **49,950
administration events over 12 months** into a time-series collection, plus a derived
near-miss history containing two deliberate clusters (a metoprolol/metformin
look-alike-name cluster on night shift, and the warfarin wrong-patient cluster on
ward 4W that powers the demo's memory beat). Every seeded row carries
`provenance: "seeded_synthetic"`; everything recorded live carries
`"observed_today"` — and the UI badges them differently.

### `memory.py` + `test_memory.py` — retrieval that changes behavior
`escalate(verdict, drugs, ward)` runs a `$match` → `$facet` aggregation over
`near_misses` (count, by-shift, by-type, three most recent with provenance). If the
count clears a threshold within a 90-day window, VERIFIED becomes
PHARMACIST_REVIEW, with the retrieved evidence attached to the verification record.
Unit-tested guarantees: it **never downgrades** a verdict, and MEMORY OFF is a true
no-op (the live counterfactual toggle).

### `api.py` — the spine
- `POST /verify` / `POST /demo/trigger`: strips any inbound verdict (perception can
  never assert one), adjudicates, escalates, persists, emits SSE.
- The mock hospital surface with the **asymmetry**: `POST /cabinet/hold` is allowed
  autonomously; `POST /cabinet/unlock` and `POST /mar/administer` return 403 until a
  named pharmacist approves. Approve **and Decline** are both first-class, ledgered
  actions; after either, the agent re-reads the patient/ward context from MongoDB
  and emits a plain-language note for the care team.
- `GET /memory/ask`: nemotron-3-nano writes a MongoDB pipeline from plain English.
  Validated hard: stage whitelist plus a **recursive operator ban** (`$where`,
  `$function`, `$accumulator`, `$expr`, `$out`, `$merge`, `$lookup` anywhere in the
  pipeline), `maxTimeMS`, and a canned fallback when the model's pipeline errors or
  returns nothing.
- `GET /patients_seen`, `GET /patients/{mrn}/history` (powers the knowledge graph,
  including computed AI insights such as the look-alike-surname alert),
  `GET /ward/{ward}/safety`, `GET /ledger/verify`, `GET /metrics`, SSE `/events`.
- Spoken warnings are rendered with Piper TTS if it is installed (`MEDPASS_PIPER`,
  `MEDPASS_PIPER_VOICE`); without it the UI shows the transcript only.

### `agent_watch.py` — the always-on loop (OpenClaw)
Opens a **change stream** on `verifications`. On BLOCKED/REVIEW it requests the
cabinet hold *over HTTP* (its egress is the policy path), wakes the OpenClaw agent
via `POST /hooks/wake` (Bearer), and reports each step to `POST /agent/activity`,
which the UI animates — so the "agent woken by the database" moment on screen is the
real event, not a scripted animation. OpenClaw's standing orders
(`config/AGENTS.md`) encode the product rule: it may block; it may never approve.

### `ui.html` — the whole front end in one file
Warm cream aesthetic. Live MJPEG camera with a scan-line sweep; on each scan the pane
freezes on the exact analyzed frame with tight bounding boxes, then returns to live.
A 104px verdict card (always color + text, never color alone). The **pixel-art agent
strip**: 8-bit MongoDB/Memory/OpenClaw sprites with data particles flowing
continuously, an amber burst + index/docs/latency ticker at each retrieval, and a
carrier that visibly walks each pharmacist decision back into the database. The
**ALL PATIENTS knowledge graph**: radial SVG per patient (meds with ward-risk,
latest 5 events with ✓/✗ decision nodes attached, "+N older checks in memory"),
click any node for a typed AI insight computed from live retrieval. Hash-chained
ledger rail at the bottom; restore banner on reconnect after a kill.
`ui_classic.html` is an alternative layout of the UI; the API serves `ui.html`.

---

## 3. Technology choices, and why

| Choice | Why |
|---|---|
| **MongoDB Community 8, single-node replica set** | The replica set is what unlocks **change streams** and **multi-document transactions** locally — both load-bearing here. Community edition because the system runs fully offline. |
| **Time-series collection** for administrations | A year of timestamped events with metadata — the primitive built for exactly this shape. |
| **Qwen3-VL-30B (q4) via Ollama** | A local vision-language model with deterministic settings (temp 0, seed 0). Vision must run locally: bedside video and patient data should not go to a cloud API. |
| **nemotron-3-nano:4b** for query-writing | A small local NVIDIA model that only ever *reads* through a whitelist. |
| **Tesseract + OpenCV** as second/third readers | Classical, deterministic tools with completely different failure modes than a neural model — that independence is what lets the system hold instead of guessing. |
| **OpenClaw** | Its gateway + hooks are the wake path for the change-stream loop, with standing orders defining its authority. |
| **FastAPI + SSE + one HTML file** | Smallest possible surface that still gives a real-time UI. No build step. |
| **Piper TTS** (optional) | Local speech for the spoken warning; the UI always shows the transcript. |

---

## 4. MongoDB, in depth

### The agent survives its own sandbox
There is **no state in any process**. Collections: `patients`, `med_orders`,
`pill_library`, `administrations` (time-series), `near_misses`, `verifications`,
`approvals`, `pending_approvals` (TTL 1h), `ledger`, `state` (cabinet + the demo
clock itself). Kill the API, the agent, or both; restart; everything returns and the
hash chain re-verifies over the same rows. The UI proves it on reconnect with the
restore banner. Data lives on a named Docker volume, so even the container can be
recreated.

### Retrieval that changes behavior
The memory layer runs **at verify time, inside the decision path** — not as a
dashboard on the side:

```
{$match: {drug, ward, ts: {$gte: now-90d}}},
{$facet: {total: [{$count}], by_shift: [...], by_type: [...], recent: [...]}}
```

Same pills, same patient, every rule passing — and the verdict still flips to
pharmacist review because of what this query returns. The counterfactual is one
keypress (MEMORY OFF → same scan verifies). The loop closes: each live block inserts
an `observed_today` near-miss that the *next* retrieval counts, so the system
visibly gets more cautious during the demo itself. Explain stats (index used, docs
examined, latency) are surfaced in the product UI, one click away.

### Business-shaped data, not a fixture
- **Scale and shape**: 49,950 time-series events across a year — wards, shifts,
  staff, drugs — queried live (ward safety view, knowledge graph, ask-the-memory),
  never preloaded into the app.
- **Database primitives doing real work**: change streams *are* the agent's trigger;
  transactions *are* the ledger's integrity; validators enforce the safety-critical
  schemas; the TTL index expires abandoned approval requests.
- **Iterated on, continuously**: verifications, approvals, declines, near-misses and
  ledger rows append as the demo runs, each with provenance labels; the knowledge
  graph and agent notes are recomputed from the database on every interaction.

### The tamper-evident ledger
Every action — human or agent, allowed or denied — is a row:
`{seq, ts, actor, action, allowed, event_id, payload_hash, prev_hash, hash}` where
`hash = sha256(seq|ts|actor|action|allowed|payload_hash|prev_hash)`. Appends happen
inside a **transaction** (with retry on write conflict — the API and the
change-stream agent genuinely race). `verify_chain()` walks the whole chain; mutate
any historical row and it reports exactly which sequence number broke.

---

## 5. Demo flow (what each beat shows)

| Beat | What happens | What it shows |
|---|---|---|
| Green path | Maria + her pills → VERIFIED | Perception + rules; even a pass consults memory |
| The block | Maria's band + Martin's pills → red BLOCKED, spoken warning, cabinet held | Rules decide; the change stream wakes the agent |
| Decline / approve | Named pharmacist decides; agent carries it into MongoDB + posts a care-team note | Humans approve; the loop closes |
| Memory beat | Martin + his OWN meds → REVIEW anyway; MEMORY OFF → VERIFIED | Retrieval changes behavior |
| Knowledge graph | ALL PATIENTS → per-patient graph + AI insights | Live retrieval over the synthetic year |
| The kill | Ctrl-C the API, restart → restore banner, chain OK | State survives a restart |
