# MEDPASS: medication-safety agent on Dell Pro Precision GB10

**An always-on medication-safety agent: a camera checks the wristband and the medication at the
bedside, a deterministic rules engine decides, and MongoDB memory escalates risky cases.** Every
model runs locally on one Dell Pro Precision GB10, so bedside video and patient data stay on the
device. The system may block a mistake by itself, but only a named pharmacist can approve one:
**models perceive, rules decide, memory escalates, humans approve.**

## Watch it first

The overview video is under [Releases](../../releases).

## Stages: how a verification works

| Stage | What happens | Check or human gate |
|---|---|---|
| 1. Perceive | A vision model, Tesseract OCR and an OpenCV colour check read the wristband and the pills independently. | The readers must agree on the patient; if they disagree or cannot read, the verdict is HOLD and a human looks. Never a guess. |
| 2. Decide | Deterministic Python rules check patient, drug, dose and time against the synthetic orders. | No model output ever sets a verdict; the rules engine is unit-tested. |
| 3. Remember | An aggregation over 12 months of synthetic near-miss history runs at verify time. A risk pattern escalates even a passing verdict. MEMORY OFF (key `M`) shows the counterfactual. | Memory can only add human review, never remove it (tested in `test_memory.py`). |
| 4. Approve | A block holds the medication cabinet automatically. Unlocking needs a named pharmacist: Approve or Decline. | Human gate: the agent may never approve, unlock or log an administration. |
| 5. Learn | Every block becomes a near-miss the next retrieval counts; every decision is written back with a note for the care team. | Every action, allowed or denied, goes into a hash-chained ledger in MongoDB. |

Verdicts: **VERIFIED** (green, automatic) · **BLOCKED** (red, cabinet held) · **REVIEW** (amber,
memory or uncertainty escalated it) · **HOLD** (grey, could not read reliably, so a human looks).

## Hardware

**Built and run on**

| Component | Built and run on |
|---|---|
| System | Dell Pro Precision GB10 (top configuration) |
| GPU | NVIDIA GB10 Grace Blackwell superchip, 128 GB unified LPDDR5x (shared by CPU and GPU) |
| CPU | 20-core NVIDIA Grace Arm CPU (GB10) |
| Memory | 128 GB unified LPDDR5x (shared by CPU and GPU) |
| Storage | 4 TB NVMe SSD |
| OS | NVIDIA DGX OS |
| Software | MongoDB 8 (replica set, time-series, change streams, transactions), OpenClaw, Qwen3-VL 30B (local), Tesseract, OpenCV, Nemotron Nano (local), Python rules engine, FastAPI |

## What's in this repo

| Path | What |
|---|---|
| `rules.py`, `test_rules.py` | The deterministic decision engine and its tests |
| `memory.py`, `test_memory.py` | MongoDB retrieval that can escalate a verdict, and its safety tests |
| `db.py`, `seed_mongo.py` | MongoDB layer (schema, validators, hash-chained ledger) and the 49,950-event synthetic year |
| `perception.py` | Camera → vision model + OCR + OpenCV triple read; emits a contract, never a verdict |
| `api.py`, `run_api.sh` | FastAPI backend and the script that (re)starts it |
| `agent_watch.py` | The always-on agent loop, woken by a MongoDB change stream |
| `config/AGENTS.md` | OpenClaw standing orders (may block; may never approve) |
| `ui.html`, `ui_classic.html` | The web UI (served by the API) and an alternative layout |
| `data/` | Synthetic patients, orders, staff, pill library and seed near-miss history |
| `training_data/pill_database.csv` | Synthetic reference table of drugs for the two demo patients |
| `TECHNICAL.md` | Architecture and module-by-module technical write-up |
| `MONGODB.md` | Where MongoDB does the work, and where to see it |
| `RUNBOOK.md` | Full run, reset and recovery checklist |
| `LICENSE`, `THIRD_PARTY_NOTICES.md` | Licence terms and third-party notices |

## Setup (Linux, NVIDIA DGX OS)

1. Install Docker, Ollama and Tesseract OCR, and pull the two local models:
   `ollama pull qwen3-vl:30b-a3b-instruct-q4_K_M` and `ollama pull nemotron-3-nano:4b`.
2. Create `.venv` in the repo folder and install the Python packages the code imports:
   ```bash
   python3 -m venv .venv
   .venv/bin/pip install fastapi uvicorn sse-starlette pymongo requests opencv-python-headless numpy pytesseract pytest
   ```
3. Start MongoDB as a single-node replica set (enables change streams and transactions):
   ```bash
   docker run -d --name medpass-mongo -v medpass_mongo:/data/db -p 27017:27017 mongo:8 --replSet rs0
   docker exec medpass-mongo mongosh --quiet --eval 'rs.initiate()'
   ```
4. Start the local models: `OLLAMA_KEEP_ALIVE=-1 ollama serve &`
5. Seed the synthetic year (deterministic): `.venv/bin/python seed_mongo.py`
6. Start the app and the agent:
   ```bash
   ./run_api.sh
   .venv/bin/python -u agent_watch.py &
   ```
7. Open http://localhost:8000. Keys: `SPACE` verify the camera scene, `A` approve / decline,
   `M` memory on/off, `1` `2` `3` replay fixtures (labelled REPLAY), `ESC` close.
8. Run the tests: `TZ=UTC .venv/bin/python -m pytest -q` (the repo's own result: 22/22).

Optional pieces (Piper TTS for the spoken warning, an OpenClaw gateway for the agent wake), the
props, the reset steps between runs and the fallbacks are in the full checklist:
**[RUNBOOK.md](RUNBOOK.md)**.

## Known limits

- **Demo only.** All patients and data are synthetic, and the UI labels them: seeded rows show
  SEEDED, live rows show OBSERVED TODAY, replays show REPLAY. MEDPASS is not a medical device and
  is not validated for clinical use.
- The cabinet and medication administration record are mock routes in `api.py`; the
  approval gate is enforced in the app layer.
- Pill identification uses colour bands calibrated on the original props (candies standing in for
  pills) under the original lighting. Other props or lighting need recalibrating in `perception.py`.
- The camera is read through Video4Linux at `/dev/video0`, so the live path is Linux only. Without
  a camera, the replay fixtures still run.
- The spoken warning needs Piper TTS; without it the warning is shown as text only.
- The OpenClaw wake is best-effort; the cabinet hold and approval pane work without it.
- `api.py` measures network egress on two interface names from the system it was built on
  (`WAN_IFACES`); on other systems, change them to your own.
- `test_memory.py` needs MongoDB running.

## Credits

Created by the Inference team: Ahamed Fofana, Dina Himed, azhao18 and rsqian (GitHub).
The original repo is [AhamedFof/medpass](https://github.com/AhamedFof/medpass): please give it a star.
A copy of the same project is also published as `ahamedfo/medpass`; this Dell copy is made from
AhamedFof/medpass.

- Partner: NVIDIA (silicon: NVIDIA GB10 Grace Blackwell superchip).
- No other repositories are bundled in this copy. MongoDB 8, OpenClaw, Qwen3-VL 30B, Nemotron Nano,
  Tesseract, OpenCV, FastAPI and the other Python packages and optional tools are installed by you
  from their own sources, each under its own licence; see
  [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
- The original repo has no licence file; the team's code is not relicensed here (see [License](#license)).
- AI-generated assets: the overview video under Releases is narrated with an AI-generated voice.
- NVIDIA, Grace, Blackwell, DGX and Nemotron are trademarks of NVIDIA Corporation. MongoDB is a
  trademark of MongoDB, Inc. Qwen, OpenClaw, Tesseract, OpenCV, FastAPI, Ollama, Piper and other product and company names
  are trademarks of their respective owners. Their use here does not imply endorsement of Dell
  Technologies or of this demo by those owners.

## Support

Logan Lawler, Dell Technologies: logan.lawler@dell.com

## License

The team's code carries no open-source licence: all rights are reserved by its authors, and Dell
does not relicense it as MIT. Dell Inc. shares it with the authors' permission so that you can
download, run and evaluate it as a demo; no other rights are granted. Dell's changes are Copyright (c) 2026 Dell Inc. See [LICENSE](LICENSE).
Third-party components keep their own licences; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
