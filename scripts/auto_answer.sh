#!/usr/bin/env bash
# Auto-answer the most recent incoming Teams call using vars from .env
set -euo pipefail

# --- load .env (no manual exports needed)
if [[ -f ".env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
else
  echo "❌ .env not found next to this script. Run from repo root." >&2
  exit 1
fi

# --- resolve variable names you already use
MS_APP_ID="${MicrosoftAppId:-${BOT_MICROSOFT_APP_ID:-}}"
MS_APP_SECRET="${MicrosoftAppPassword:-${BOT_MICROSOFT_APP_PASSWORD:-}}"
MS_TENANT="${MicrosoftAppTenantId:-${BOT_MICROSOFT_APP_TENANT_ID:-${BOT_MICROSOFT_APP_TENANT_ID:-}}}"
PUBLIC_URL="${PUBLIC_BASE_URL:-}"
if [[ -z "$MS_APP_ID$MS_APP_SECRET$MS_TENANT$PUBLIC_URL" ]]; then
  echo "❌ Missing one of: MicrosoftAppId, MicrosoftAppPassword, MicrosoftAppTenantId, PUBLIC_BASE_URL in .env" >&2
  exit 1
fi

echo ">>> Getting Graph token for tenant: $MS_TENANT"
ACCESS_TOKEN="$(curl -s -X POST \
  "https://login.microsoftonline.com/${MS_TENANT}/oauth2/v2.0/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "client_id=${MS_APP_ID}&client_secret=${MS_APP_SECRET}&grant_type=client_credentials&scope=https%3A%2F%2Fgraph.microsoft.com%2F.default" \
  | jq -r '.access_token')"

if [[ -z "$ACCESS_TOKEN" || "$ACCESS_TOKEN" == "null" ]]; then
  echo "❌ Could not obtain ACCESS_TOKEN. Check app id/secret/tenant." >&2
  exit 1
fi
echo "Token OK. Waiting for an incoming call…"

# --- poll ngrok for the latest /calls/notifications with changeType 'created'
NGROK_API="http://127.0.0.1:4040/api/requests/http"
CALL_ID=""
for _ in {1..20}; do
  CALL_ID="$(
    curl -s "$NGROK_API" | jq -r '
      .requests
      | map(select((.request.uri // .uri) | endswith("/calls/notifications")))
      | sort_by(.start) | reverse
      | map( try ((.request.body // .body) | fromjson) catch {} )
      | map( select(.value and (.value|type=="array")) | .value[] )
      | map( select(.changeType=="created") )
      | map( .resourceData.id // empty )
      | .[0] // empty
    '
  )"
  [[ -n "$CALL_ID" ]] && break
  sleep 0.5
done

if [[ -z "$CALL_ID" ]]; then
  echo "❌ Could not find a recent 'created' notification. Make a NEW call to the bot, then re-run." >&2
  exit 1
fi
echo ">>> Answering call: $CALL_ID"

# --- answer
HTTP_CODE="$(curl -s -o /tmp/answer.out -w "%{http_code}" -X POST \
  "https://graph.microsoft.com/v1.0/communications/calls/${CALL_ID}/answer" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{
        \"callbackUri\": \"${PUBLIC_URL}/calls/notifications\",
        \"acceptedModalities\": [\"audio\"],
        \"mediaConfig\": { \"@odata.type\": \"#microsoft.graph.serviceHostedMediaConfig\" }
      }")"

echo "[CALL] Answer -> ${HTTP_CODE}"
cat /tmp/answer.out && echo

if [[ "$HTTP_CODE" != "200" && "$HTTP_CODE" != "204" ]]; then
  echo "⚠️ Graph did not return 200/204. See payload above. If you see 7505, the token tenant doesn't match the call tenant." >&2
  exit 1
fi
