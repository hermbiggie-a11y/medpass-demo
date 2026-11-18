"""MEDPASS always-on agent loop - driven BY MongoDB, not by polling.

A change stream on `verifications` wakes this watcher the instant a verdict is
inserted. On BLOCKED / PHARMACIST_REVIEW it requests the cabinet hold through the
same HTTP surface a human would use (its egress IS the policy path), and wakes the
OpenClaw agent over its hooks endpoint so the on-call channel carries the incident.

The unlock is deliberately NOT here: this process may block an error by itself;
it may never approve one by itself.

Run: .venv/bin/python agent_watch.py
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

import requests as http

from db import get_db, watch_verifications

API = os.environ.get("MEDPASS_API", "http://localhost:8000")
OPENCLAW_HOOK = os.environ.get("OPENCLAW_HOOK", "http://localhost:18789/hooks/wake")
OPENCLAW_TOKEN = os.environ.get("OPENCLAW_TOKEN", "medpass-demo-token")


def report(stage: str, event_id: str | None, detail: str) -> None:
    """Tell the canvas what the watcher ACTUALLY did - the UI animates only this."""
    try:
        http.post(f"{API}/agent/activity",
                  json={"stage": stage, "event_id": event_id, "detail": detail}, timeout=3)
    except Exception:
        pass


def act_on(record: dict) -> None:
    verdict = record.get("verdict")
    event_id = record.get("event_id")
    print(f"[agent] {datetime.now(timezone.utc).isoformat()} saw {event_id} -> {verdict}")
    report("change_stream_wake", event_id, f"change stream delivered {verdict}")
    if verdict not in ("BLOCKED", "PHARMACIST_REVIEW"):
        return

    # 1. Hold the cabinet - autonomous, allowed by policy.
    try:
        r = http.post(f"{API}/cabinet/hold",
                      json={"event_id": event_id, "actor": "openclaw-agent"}, timeout=5)
        print(f"[agent] cabinet/hold -> {r.status_code}")
        report("cabinet_hold", event_id, "agent requested cabinet hold via HTTP")
    except Exception as e:
        print(f"[agent] cabinet/hold failed: {e}")

    # 2. Wake the OpenClaw agent so a human is pulled in. Best-effort: the demo's
    #    approval pane works regardless; this is the channel escalation.
    try:
        r = http.post(OPENCLAW_HOOK,
                      headers={"Authorization": f"Bearer {OPENCLAW_TOKEN}"},
                      json={"text": f"MEDPASS {verdict}: {record.get('summary')} "
                                    f"(event {event_id}, patient {record.get('patient', {}).get('name')}). "
                                    f"Pharmacist approval required on the MEDPASS screen."},
                      timeout=5)
        print(f"[agent] openclaw wake -> {r.status_code}")
        report("openclaw_wake", event_id, "openclaw agent woken via /hooks/wake")
    except Exception as e:
        print(f"[agent] openclaw wake unreachable ({e}) - approval pane still active")


def main() -> None:
    db = get_db()
    print("[agent] watching MongoDB change stream on verifications ...")
    while True:
        try:
            with watch_verifications(db) as stream:
                for change in stream:
                    doc = change.get("fullDocument") or {}
                    doc.pop("_id", None)
                    act_on(json.loads(json.dumps(doc, default=str)))
        except Exception as e:
            print(f"[agent] stream error, reconnecting: {e}")
            time.sleep(2)


if __name__ == "__main__":
    main()
