#!/usr/bin/env bash
set -euo pipefail

# === Requires ===
# - ngrok running with inspection:  ngrok http --inspect=true 8000
# - uvicorn running your bot on port 8000
# - jq installed
# - .env already sourced in this shell
# - PUBLIC_BASE_URL set to your ngrok HTTPS URL

: "${PUBLIC_BASE_URL:?Set PUBLIC_BASE_URL (your ngrok https URL)}"
: "${BOT_MICROSOFT_APP_ID:?Set in .env}"
: "${BOT_MICROSOFT_APP_PASSWORD:?Set in .env}"
: "${BOT_MICROSOFT_APP_TENANT_ID:?Set in .env}"

NGROK_API="http://127.0.0.1:4040/api/requests/http"

get_token() {
  curl -s "https://login.microsoftonline.com/${BOT_MICROSOFT_APP_TENANT_ID}/oauth2/v2.0/token" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "client_id=${BOT_MICROSOFT_APP_ID}&client_secret=${BOT_MICROSOFT_APP_PASSWORD}&grant_type=client_credentials&scope=https%3A%2F%2Fgraph.microsoft.com%2F.default" \
  | jq -r '.access_token'
}

get_latest_req_id() {
  curl -s "$NGROK_API" | jq -r '
    .requests
    | map(select((.request.uri // .uri) | endswith("/calls/notifications")))
    | sort_by(.start) | last
    | .id // empty
  '
}

get_call_id_from_req() {
  local req_id="$1"
  curl -s "$NGROK_API/$req_id" | jq -r '
    (.request.body // .body // .raw // "")
    | try (fromjson | .value[]? | select(.changeType=="created") | .resourceData.id) // empty
  '
}

answer_call() {
  local call_id="$1" token="$2"
  curl -s -o /dev/null -w "Graph /answer → HTTP %{http_code}\n" \
    -X POST "https://graph.microsoft.com/v1.0/communications/calls/${call_id}/answer" \
    -H "Authorization: Bearer ${token}" \
    -H "Content-Type: application/json" \
    -d '{
          "callbackUri": "'"${PUBLIC_BASE_URL}"'/calls/notifications",
          "acceptedModalities": ["audio"],
          "mediaConfig": { "@odata.type": "#microsoft.graph.serviceHostedMediaConfig" }
        }'
}

main() {
  echo ">>> Fetching latest /calls/notifications request id from ngrok…"
  local req_id
  req_id="$(get_latest_req_id || true)"
  if [[ -z "$req_id" ]]; then
    echo "No /calls/notifications found. Start a call to your bot, then re-run."
    exit 1
  fi
  echo "REQ_ID=$req_id"

  echo ">>> Extracting callId from that request…"
  local call_id
  call_id="$(get_call_id_from_req "$req_id" || true)"
  if [[ -z "$call_id" ]]; then
    echo "Could not find a 'created' notification with a callId. Start a NEW call and re-run quickly."
    exit 1
  fi
  echo "CALLID=$call_id"

  echo ">>> Getting Graph access token…"
  local token
  token="$(get_token)"
  if [[ -z "$token" || "$token" == "null" ]]; then
    echo "Failed to obtain token. Check BOT_MICROSOFT_* in .env."
    exit 1
  fi
  echo "Token OK."

  echo ">>> Answering call…"
  answer_call "$call_id" "$token"
}

main "$@"
