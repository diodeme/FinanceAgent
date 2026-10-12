#!/usr/bin/env bash
# Seed the AgentScope service with a credential, an agent and a session.
# Idempotent: reuses existing entries by name. Uses curl + jq.
#
# Usage: ./scripts/seed.sh [API_BASE] [USER_ID]
#   API_BASE  defaults to http://localhost:8000
#   USER_ID   defaults to dev

set -euo pipefail

API_BASE="${1:-http://localhost:8000}"
USER_ID="${2:-dev}"

# Load config from ../.env (repo root)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
set -a; source "$SCRIPT_DIR/../.env"; set +a

H="X-User-ID: $USER_ID"
CT="Content-Type: application/json"
BASE="$API_BASE"

echo "== credential =="
CRED_ID="$(curl -sf "$BASE/credential/" -H "$H" \
  | jq -r --arg key "$DEEPSEEK_API_KEY" '.credentials[] | select(.data.api_key == $key) | .id' | head -1)"
if [ -z "$CRED_ID" ] || [ "$CRED_ID" = "null" ]; then
  CRED_ID="$(curl -sf -X POST "$BASE/credential/" -H "$H" -H "$CT" \
    -d "{\"data\":{\"type\":\"deepseek_credential\",\"api_key\":\"$DEEPSEEK_API_KEY\",\"base_url\":\"$DEEPSEEK_BASE_URL\"}}" \
    | jq -r '.credential_id')"
  echo "created $CRED_ID"
else
  echo "reused $CRED_ID"
fi

echo "== agent =="
AGENT_ID="$(curl -sf "$BASE/agent/" -H "$H" | jq -r '.agents[] | select(.name == "FinanceAgent") | .agent_id' | head -1)"
if [ -z "$AGENT_ID" ] || [ "$AGENT_ID" = "null" ]; then
  AGENT_ID="$(curl -sf -X POST "$BASE/agent/" -H "$H" -H "$CT" \
    -d '{"name":"FinanceAgent","system_prompt":"You are a financial research assistant for equity analysts. Be concise."}' \
    | jq -r '.agent_id')"
  echo "created $AGENT_ID"
else
  echo "reused $AGENT_ID"
fi

echo "== session =="
SESSION_ID="$(curl -sf "$BASE/sessions/?agent_id=$AGENT_ID" -H "$H" \
  | jq -r '.sessions[]? | .session_id' | head -1)"
if [ -z "$SESSION_ID" ] || [ "$SESSION_ID" = "null" ]; then
  SESSION_ID="$(curl -sf -X POST "$BASE/sessions/" -H "$H" -H "$CT" \
    -d "{\"agent_id\":\"$AGENT_ID\",\"name\":\"smoke-test\",\"chat_model_config\":{\"type\":\"deepseek_credential\",\"credential_id\":\"$CRED_ID\",\"model\":\"$DEEPSEEK_MODEL\",\"parameters\":{}}}" \
    | jq -r '.session_id')"
  echo "created $SESSION_ID"
else
  echo "reused $SESSION_ID"
fi

echo
echo "credential_id=$CRED_ID"
echo "agent_id=$AGENT_ID"
echo "session_id=$SESSION_ID"
