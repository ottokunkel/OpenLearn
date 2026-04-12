#!/usr/bin/env bash
set -euo pipefail

# ── Config ──────────────────────────────────────────────
BASE_URL="${MODAL_ENDPOINT_URL:?Set MODAL_ENDPOINT_URL to your deployed Modal web endpoint}"
MODEL="llm"
APP_NAME="vlm-endpoint-docworker-v1"

# ── Check live containers ───────────────────────────────
echo "==> Live containers for ${APP_NAME}:"
CONTAINER_COUNT=$(modal container list -a "$APP_NAME" 2>/dev/null | grep -c "Running" || true)
echo "    Running containers: ${CONTAINER_COUNT}"

if [ "$CONTAINER_COUNT" -eq 0 ]; then
  echo "    No containers running — this will be a COLD START"
else
  echo "    Containers already warm — this will be a WARM START"
fi

# ── Health check (timed) ────────────────────────────────
echo ""
echo "==> Health check: ${BASE_URL}/health"
START=$(date +%s%3N 2>/dev/null || python3 -c 'import time; print(int(time.time()*1000))')

HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "${BASE_URL}/health")

END=$(date +%s%3N 2>/dev/null || python3 -c 'import time; print(int(time.time()*1000))')
HEALTH_MS=$((END - START))

if [ "$HTTP_CODE" -eq 200 ]; then
  echo "    PASS (HTTP $HTTP_CODE) — ${HEALTH_MS}ms"
else
  echo "    FAIL (HTTP $HTTP_CODE) — ${HEALTH_MS}ms"
  exit 1
fi

# ── Chat completion (timed) ─────────────────────────────
echo ""
echo "==> Chat completion: ${BASE_URL}/v1/chat/completions"
START=$(date +%s%3N 2>/dev/null || python3 -c 'import time; print(int(time.time()*1000))')

RESPONSE=$(curl -s -w "\n%{http_code}" "${BASE_URL}/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d "{
    \"model\": \"${MODEL}\",
    \"messages\": [
      {\"role\": \"system\", \"content\": \"You are a helpful assistant.\"},
      {\"role\": \"user\", \"content\": \"Say hello in one sentence.\"}
    ],
    \"stream\": false
  }")

END=$(date +%s%3N 2>/dev/null || python3 -c 'import time; print(int(time.time()*1000))')
CHAT_MS=$((END - START))

HTTP_CODE=$(echo "$RESPONSE" | tail -1)
BODY=$(echo "$RESPONSE" | sed '$d')

if [ "$HTTP_CODE" -eq 200 ]; then
  echo "    PASS (HTTP $HTTP_CODE) — ${CHAT_MS}ms"
  echo "    Response:"
  echo "$BODY" | python3 -m json.tool 2>/dev/null || echo "$BODY"
else
  echo "    FAIL (HTTP $HTTP_CODE) — ${CHAT_MS}ms"
  echo "$BODY"
  exit 1
fi

# ── Summary ─────────────────────────────────────────────
echo ""
echo "==> Summary"
echo "    Start type:      $([ "$CONTAINER_COUNT" -eq 0 ] && echo 'COLD' || echo 'WARM')"
echo "    Health check:    ${HEALTH_MS}ms"
echo "    Chat completion: ${CHAT_MS}ms"
echo "    Total:           $((HEALTH_MS + CHAT_MS))ms"
