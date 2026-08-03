#!/bin/bash
source /root/pytorch_rocm/bin/activate
cd /root/DART
nohup uvicorn server:app --host 0.0.0.0 --port 8900 > /tmp/dart_server.log 2>&1 &
SERVER_PID=$!
echo "Server PID: $SERVER_PID"
# Keep WSL alive while server runs
wait $SERVER_PID
