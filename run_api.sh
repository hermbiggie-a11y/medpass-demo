#!/bin/bash
# The one true way to (re)start the MEDPASS API. Kills every stale instance first
# so exactly one process owns port 8000 AND the camera.
cd "$(dirname "$0")"
pkill -9 -f 'uvicorn api:app' 2>/dev/null
sleep 1.5
nohup .venv/bin/uvicorn api:app --host 0.0.0.0 --port 8000 \
  >> /tmp/medpass_api.log 2>&1 &
sleep 4
curl -s -o /dev/null -w "api: %{http_code}\n" localhost:8000/metrics
timeout 3 curl -s -o /tmp/medpass_stream_test.bin localhost:8000/camera/stream
b=$(wc -c < /tmp/medpass_stream_test.bin 2>/dev/null || echo 0)
echo "camera stream bytes in 3s: $b"
