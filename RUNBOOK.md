# MEDPASS — run, reset and recover

The full checklist for running MEDPASS on Dell Pro Precision GB10 (NVIDIA DGX OS, Linux).
Demo only: every patient, order and event is synthetic, and MEDPASS is not a medical device.

## 1. Before you start (one time)

- [ ] Docker is installed and your user can run `docker` without `sudo`.
- [ ] Ollama is installed and running, with both models pulled:
      `ollama pull qwen3-vl:30b-a3b-instruct-q4_K_M` and `ollama pull nemotron-3-nano:4b`.
      Keep them loaded with `OLLAMA_KEEP_ALIVE=-1` so there are no cold reloads mid-demo.
- [ ] Tesseract OCR is installed (for example `sudo apt-get install -y tesseract-ocr`).
      If you use an unpacked copy instead, point `MEDPASS_TESS_ROOT` at it.
- [ ] A Python virtual environment exists in the repo folder as `.venv` with the packages the
      code imports: `fastapi`, `uvicorn`, `sse-starlette`, `pymongo`, `requests`,
      `opencv-python-headless`, `numpy`, `pytesseract`, `pytest`.
- [ ] A USB camera shows up as `/dev/video0`.
- [ ] Optional: Piper TTS for the spoken warning (`MEDPASS_PIPER` = the `piper` binary,
      `MEDPASS_PIPER_VOICE` = the `en_US-amy-medium.onnx` voice). Without it the UI shows the
      warning as text.
- [ ] Optional: an OpenClaw gateway with hooks enabled on `localhost:18789`, using
      `config/AGENTS.md` as the agent's standing orders. Set `OPENCLAW_TOKEN` to the gateway's
      hook token. Without it the agent loop still holds the cabinet and the approval pane still
      works; only the OpenClaw wake is skipped.
- [ ] Props: a printed wristband for each demo patient (MARIA GONZALES, MRN-482913 and
      MARTIN GONZALES, MRN-482931) and visually distinct items standing in for each patient's
      medication. The colour bands in `perception.py` were calibrated on the original props
      under the original lighting; check them against yours.

## 2. Start

1. Start MongoDB as a single-node replica set (needed for change streams and transactions):
   ```bash
   docker run -d --name medpass-mongo -v medpass_mongo:/data/db -p 27017:27017 mongo:8 --replSet rs0
   docker exec medpass-mongo mongosh --quiet --eval 'rs.initiate()'
   ```
   On later runs, `docker start medpass-mongo` is enough.
2. Make sure Ollama is serving: `OLLAMA_KEEP_ALIVE=-1 ollama serve &` (skip if it runs as a service).
3. Seed the synthetic year: `.venv/bin/python seed_mongo.py`
4. Start the API: `./run_api.sh` — it prints the API status code and how many camera bytes
   arrived in 3 seconds. Zero bytes means the camera is not being read.
5. Start the agent loop: `.venv/bin/python -u agent_watch.py &`
6. Open http://localhost:8000

## 3. Check it works

- [ ] Tests pass: `TZ=UTC .venv/bin/python -m pytest -q` (the repo's own result is 22/22;
      `test_memory.py` needs MongoDB running).
- [ ] The top bar shows the seeded event count from MongoDB.
- [ ] Key `1` replays a fixture and shows a verdict with an amber REPLAY chip.
- [ ] `SPACE` with a wristband and a cup in front of the camera produces a verdict card.

| Key | Does |
|---|---|
| `SPACE` | verify the live camera scene |
| `A` | approve / decline pane |
| `M` | memory on/off (the counterfactual) |
| `1` `2` `3` | replay fixtures (labelled REPLAY) |
| `ESC` | close / unfocus |

## 4. Reset between runs

1. `.venv/bin/python seed_mongo.py` — rebuilds the synthetic world so counts start from the
   same point every run.
2. `./run_api.sh` — restarts the API (it stops any stale instance first, so only one process
   owns port 8000 and the camera).
3. Stop and restart `agent_watch.py`.

## 5. If something goes wrong

| Problem | What to do |
|---|---|
| Camera not read (0 bytes from `run_api.sh`) | Check `/dev/video0`, close any other app using the camera, run `./run_api.sh` again. Meanwhile use keys `1` / `2` / `3`; the REPLAY chip shows, so say it is a replayed capture. Never present a replay as live. |
| A scan reads wrong or holds | That is the design: when the readers disagree it holds and asks a human instead of guessing. Re-present the wristband and cup, or use a replay key. |
| Verdict never arrives | Check Ollama is serving and the vision model is loaded (`ollama list`); perception degrades to HOLD if the model fails. |
| "Ask the memory" answers from a canned query | The local model's pipeline failed validation or returned nothing; the canned fallback is expected behaviour. |
| No spoken warning | Piper is not installed or not found; the warning text still shows. |
| OpenClaw wake fails in the agent log | Optional; the cabinet hold and approval pane still work. Check the gateway and `OPENCLAW_TOKEN`. |
| API down | `./run_api.sh` restarts it; on reconnect the UI shows "STATE RESTORED FROM MONGODB" because all state lives in MongoDB. |
| MongoDB errors about change streams or transactions | The replica set was not initiated: run the `rs.initiate()` line in step 2.1. |
