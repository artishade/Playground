#!/bin/bash
# Startup script for Genymotion Emulator MCP integration
# Starts the emulator control server, then optionally registers the MCP server

set -e

# Defaults — override via environment
GENY_WEBRTC_URL="${GENY_WEBRTC_URL:-}"
GENY_TOKEN="${GENY_TOKEN:-}"
GENY_API_TOKEN="${GENY_API_TOKEN:-}"
GENY_INSTANCE_UUID="${GENY_INSTANCE_UUID:-}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Starting Genymotion Emulator Control Server..."
python3 "${SCRIPT_DIR}/emulator_control_server.py" \
  --webrtc-url "$GENY_WEBRTC_URL" \
  --token "$GENY_TOKEN" \
  --api-token "$GENY_API_TOKEN" \
  --instance-uuid "$GENY_INSTANCE_UUID" \
  --port 8080 &

CONTROL_PID=$!
echo "Control server started (PID: $CONTROL_PID)"

# Wait for control server to be ready
sleep 2
if curl -s http://localhost:8080/ > /dev/null 2>&1; then
    echo "Control server is healthy"
else
    echo "WARNING: Control server not responding on port 8080"
fi

# Keep container running
wait $CONTROL_PID
