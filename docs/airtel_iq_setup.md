# Airtel IQ API Setup

This project now includes an Airtel IQ provider scaffold alongside Twilio and Exotel.

Because Airtel IQ account setups can differ, the integration is intentionally configurable:

- use either `AIRTEL_IQ_API_URL` (single initiate URL) or `AIRTEL_IQ_BASE_URL` + path keys
- request headers are shaped with `AIRTEL_IQ_HEADERS_JSON`
- the outbound request body is shaped with `AIRTEL_IQ_REQUEST_TEMPLATE_JSON`

## 1. Environment variables

Add these to `.env`:

```bash
TELEPHONY_PROVIDER=airtel_iq
PUBLIC_BASE_URL=https://your-public-host.example.com

# Use one of the two endpoint modes below:
# Mode A (single full initiate URL):
AIRTEL_IQ_API_URL=https://your-airtel-iq-api.example.com/outbound/call
# Mode B (base URL + action paths):
AIRTEL_IQ_BASE_URL=https://your-airtel-iq-api.example.com
AIRTEL_IQ_INITIATE_PATH=/voice/call/initiate
AIRTEL_IQ_PLAY_AUDIO_PATH=/voice/call/playAudio
AIRTEL_IQ_COLLECT_INPUT_PATH=/voice/call/collectInput
AIRTEL_IQ_HANGUP_PATH=/voice/call/hangup

AIRTEL_IQ_API_KEY=your_airtel_iq_api_key
AIRTEL_IQ_API_SECRET=your_airtel_iq_api_secret
AIRTEL_IQ_APPLICATION_ID=your_airtel_iq_application_id
AIRTEL_IQ_CALLER_ID=your_airtel_iq_caller_id

AIRTEL_IQ_HEADERS_JSON={"Authorization":"Bearer {api_key}","Content-Type":"application/json"}
AIRTEL_IQ_REQUEST_TEMPLATE_JSON={"to":"{to_number}","from":"{caller_id}","applicationId":"{application_id}","callbackUrl":"{events_callback_url}","statusCallbackUrl":"{status_callback_url}","cdrUrl":"{cdr_callback_url}","mediaUrl":"{ws_url}","metaData":{"pending_id":"{pending_id}","client_id":"{client_id}","project_id":"{project_id}","provider":"{provider}","direction":"{direction}","call_direction":"{call_direction}","outreach_mode":"{outreach_mode}","to_number":"{to_number}","caller_id":"{caller_id}"}}
```

Optional internal costing:

```bash
AIRTEL_IQ_COST_PER_MINUTE=0
```

## 2. Request placeholders

The Airtel IQ request template supports these placeholders:

- `{to_number}`
- `{caller_id}`
- `{application_id}`
- `{events_callback_url}`
- `{status_callback_url}`
- `{cdr_callback_url}`
- `{callback_url}` (alias for `{events_callback_url}`)
- `{cdr_url}` (alias for `{cdr_callback_url}`)
- `{ws_url}`
- `{api_key}`
- `{api_secret}`
- `{public_base_url}`

Example:

```json
{
  "to": "{to_number}",
  "from": "{caller_id}",
  "applicationId": "{application_id}",
  "callbackUrl": "{events_callback_url}",
  "statusCallbackUrl": "{status_callback_url}",
  "cdrUrl": "{cdr_callback_url}",
  "mediaUrl": "{ws_url}",
  "metaData": {
    "pending_id": "{pending_id}",
    "client_id": "{client_id}",
    "project_id": "{project_id}",
    "provider": "{provider}",
    "direction": "{direction}",
    "call_direction": "{call_direction}",
    "outreach_mode": "{outreach_mode}",
    "to_number": "{to_number}",
    "caller_id": "{caller_id}"
  }
}
```

## 3. Public endpoints Airtel IQ can reach

This app exposes these Airtel IQ-facing routes:

- `POST /airtel-iq/status/{pending_id}`
- `POST /airtel-iq/events/{pending_id}`
- `POST /airtel-iq/cdr/{pending_id}`
- `WS /airtel-iq/ws-airtel/`

What they do:

- `/airtel-iq/status/{pending_id}` captures status payloads.
- `/airtel-iq/events/{pending_id}` captures real-time event payloads (for example `CALL_CONNECTED`, `PROMPT_COMPLETED`, `USER_INPUT_RECEIVED`, `CALL_DISCONNECTED`).
- `/airtel-iq/cdr/{pending_id}` captures call detail records (duration/disposition/etc).
- `/airtel-iq/ws-airtel/` is the static media WebSocket endpoint for Airtel IQ. The call session is resolved from `start.customParameters.pending_id`.

## 4. Important note on the media bridge

The current Airtel IQ media bridge is implemented as a configurable scaffold.

It currently assumes an Exotel-like websocket event shape:

- `connected`
- `start`
- `media`
- `clear`
- `stop`

and base64-encoded PCM payloads in `message.media.payload`.

If your Airtel IQ account uses a different websocket schema, update:

- `voice_sales_agent/telephony.py`
- `voice_sales_agent/web_app.py`

## 5. What the app sends

When you click `Call`, the app builds:

- `events_callback_url = <PUBLIC_BASE_URL>/airtel-iq/events/<pending_id>`
- `status_callback_url = <PUBLIC_BASE_URL>/airtel-iq/status/<pending_id>`
- `cdr_callback_url = <PUBLIC_BASE_URL>/airtel-iq/cdr/<pending_id>`
- `ws_url = wss://<PUBLIC_BASE_URL host>/airtel-iq/ws-airtel/`
- `metaData` includes `pending_id`, `client_id`, `project_id`, `provider`, `direction`, `call_direction`, `outreach_mode`, `to_number`, and `caller_id`

and injects those into the configured request template before POSTing to:

- `AIRTEL_IQ_API_URL` when provided, otherwise
- `AIRTEL_IQ_BASE_URL + AIRTEL_IQ_INITIATE_PATH`

## 6. Required Airtel IQ details and where to get them

| Required detail | Env key / usage | Where to get it |
|---|---|---|
| Airtel IQ API endpoint | `AIRTEL_IQ_API_URL` (single URL mode) | Airtel IQ onboarding/API handbook for your tenant (or from your Airtel solution engineer). |
| Airtel IQ API base URL | `AIRTEL_IQ_BASE_URL` (base URL mode) | Airtel IQ onboarding/API handbook for your tenant (or from your Airtel solution engineer). |
| Initiate/play/collect/hangup endpoint paths | `AIRTEL_IQ_INITIATE_PATH`, `AIRTEL_IQ_PLAY_AUDIO_PATH`, `AIRTEL_IQ_COLLECT_INPUT_PATH`, `AIRTEL_IQ_HANGUP_PATH` | Airtel IQ Voice API reference for your provisioned tenant version. |
| Auth key/token | `AIRTEL_IQ_API_KEY` | Airtel IQ developer credentials panel / credential handover mail. |
| Auth secret (if applicable) | `AIRTEL_IQ_API_SECRET` | Airtel IQ credential handover (only if your contract uses key+secret). |
| Voice app / flow id | `AIRTEL_IQ_APPLICATION_ID` | Airtel IQ Voice application configuration in your tenant. |
| Outbound caller id / virtual number | `AIRTEL_IQ_CALLER_ID` | Airtel-provisioned virtual number assigned to your account. |
| Auth header shape | `AIRTEL_IQ_HEADERS_JSON` | Airtel API auth examples for your account (Bearer, x-api-key, etc). |
| Initiate-call request shape | `AIRTEL_IQ_REQUEST_TEMPLATE_JSON` | Airtel “initiate call” API sample payload for your account. |
| Public callback host | `PUBLIC_BASE_URL` | Your own infra/tunnel URL (must be publicly reachable over HTTPS). |
| Media event schema (if streaming) | runtime parsing in `web_app.py` / `telephony.py` | Airtel IQ event schema docs for your specific streaming product version. |

Use your Airtel account’s exact API examples as source of truth. The field names are not always identical across Airtel IQ products/versions.

## 7. Quick validation checklist

- `TELEPHONY_PROVIDER=airtel_iq`
- `PUBLIC_BASE_URL` is public and HTTPS
- `AIRTEL_IQ_API_URL` is correct, or `AIRTEL_IQ_BASE_URL` + `AIRTEL_IQ_INITIATE_PATH` is correct
- `AIRTEL_IQ_HEADERS_JSON` matches the auth contract Airtel IQ expects
- `AIRTEL_IQ_REQUEST_TEMPLATE_JSON` matches the body Airtel IQ expects
- Airtel IQ can reach `/airtel-iq/events/{pending_id}`
- Airtel IQ can reach `/airtel-iq/status/{pending_id}`
- Airtel IQ can reach `/airtel-iq/cdr/{pending_id}`
- Airtel IQ can reach `wss://<public-host>/airtel-iq/ws-airtel/` if your account supports live media streaming

## 8. Relevant code locations

- `voice_sales_agent/config.py`
- `voice_sales_agent/telephony.py`
- `voice_sales_agent/web_app.py`
