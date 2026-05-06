# Gemini Voice Sales Agent Demo

Project handbook (single source of truth): [`docs/project_handbook.md`](/Users/idriskhan/Documents/new_voice_agent/docs/project_handbook.md)

## Architecture summary

This project is a local, demo-ready voice sales agent built around the Gemini Live API for real-time audio conversations and a separate Gemini structured generation call for post-call extraction. The architecture keeps the live voice loop, prompt assets, client configs, and reporting isolated so new clients can be onboarded through files and scripts instead of code edits.

Key design choices:

- `voice_sales_agent/gemini_api.py` isolates Gemini Live and structured generation so SDK or model upgrades stay in one place.
- `voice_sales_agent/audio.py` owns local microphone/speaker transport and includes explicit TODO hooks for future telephony integration.
- `voice_sales_agent/web_app.py` adds a minimal local dashboard that controls the same session engine used by the CLI.
- `clients/<client_id>/...` stores client-specific prompts, knowledge, objections, qualification rules, and CTA settings.
- `voice_sales_agent/prompt_builder.py` composes global instructions plus client assets into the session prompt.
- `voice_sales_agent/session.py` orchestrates the live loop, turn logging, status updates, latency capture, and post-call summaries.
- `scripts/create_client.py` makes onboarding new clients a file-driven workflow.

For the current Gemini path, the demo is built around a real-time audio session with server-side turn-taking and synthesized voice output. Post-call extraction is intentionally separated into a fast follow-up step so the conversation path stays as low-latency as possible.

## Folder structure

```text
new_voice_agent/
├── .env.example
├── README.md
├── requirements.txt
├── clients/
│   ├── acme_health/
│   └── nova_security/
├── prompts/
│   └── global_system.txt
├── scripts/
│   ├── create_client.py
│   ├── run_demo.py
│   └── validate_client.py
├── sessions/
├── logs/
└── voice_sales_agent/
    ├── audio.py
    ├── cli.py
    ├── clients.py
    ├── config.py
    ├── constants.py
    ├── extraction.py
    ├── gemini_api.py
    ├── memory.py
    ├── models.py
    ├── prompt_builder.py
    ├── session.py
    ├── transcripts.py
    ├── web_app.py
    ├── web_state.py
    └── web_static/
```

## Features

- Local microphone input and local speaker playback
- Gemini Live audio session for near-real-time sales conversations
- Client-specific prompts and knowledge loaded from files
- Two sample clients included
- Transcript logging plus JSON and Markdown session outputs
- Post-call structured extraction for lead info, objections, qualification, and next action
- Latency capture and simple CLI status indicators
- Minimal local web dashboard for client selection, session control, transcript viewing, and metrics
- Optional Twilio or Exotel outbound calling path for dialing a real mobile phone
- Scripts for client creation, client validation, and running a demo

## Requirements

- Python 3.11+
- A Gemini API key (and optionally a Sarvam API key for Sarvam TTS/STT)
- PortAudio installed locally for `PyAudio`

Common PortAudio install commands:

- macOS with Homebrew: `brew install portaudio`
- Ubuntu/Debian: `sudo apt-get install portaudio19-dev`

## Setup

1. Create a virtual environment and activate it.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Copy `.env.example` to `.env` and fill in your Gemini key:

```bash
cp .env.example .env
```

4. Set:

- `GEMINI_API_KEY`
- `SPEECH_PROVIDER` (`gemini` or `sarvam`)
- Optional per-component provider overrides:
  - `LIVE_PROVIDER` (`gemini` or `sarvam`)
  - `STRUCTURED_PROVIDER` (`gemini` or `sarvam`)
  - `TTS_PROVIDER` (`gemini` or `sarvam`)
  - `RECORDING_STT_PROVIDER` (`recording` provider currently supports `gemini` or `sarvam`)
- If using Sarvam: `SARVAM_API_KEY`, optional `SARVAM_CHAT_MODEL`, `SARVAM_TTS_MODEL`, `SARVAM_TTS_SPEAKER`, `SARVAM_STT_MODEL`
- Optionally `GEMINI_LIVE_MODEL`
- Optionally `GEMINI_STRUCTURED_MODEL`
- Optionally `DEFAULT_CLIENT_ID`
- For phone calls: `PUBLIC_BASE_URL`, `TELEPHONY_PROVIDER`
- For Twilio: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`
- For Exotel: `EXOTEL_ACCOUNT_SID`, `EXOTEL_API_KEY`, `EXOTEL_API_TOKEN`, `EXOTEL_CALLER_ID`, `EXOTEL_SUBDOMAIN`, `EXOTEL_APP_ID`
- For Airtel IQ: `AIRTEL_IQ_API_URL`, `AIRTEL_IQ_API_KEY`, `AIRTEL_IQ_API_SECRET`, `AIRTEL_IQ_APPLICATION_ID`, `AIRTEL_IQ_CALLER_ID`, `AIRTEL_IQ_HEADERS_JSON`, `AIRTEL_IQ_REQUEST_TEMPLATE_JSON`
- For Meta WhatsApp calling (direct API path): `META_WHATSAPP_BASE_URL`, `META_WHATSAPP_API_VERSION`, `META_WHATSAPP_ACCESS_TOKEN`, `META_WHATSAPP_PHONE_NUMBER_ID`, `META_WHATSAPP_FROM_NUMBER`, `META_WHATSAPP_INITIATE_PATH`, `META_WHATSAPP_REQUEST_TEMPLATE_JSON`, `META_WHATSAPP_DEFAULT_SDP_TYPE`, `META_WHATSAPP_DEFAULT_SDP`, `META_WHATSAPP_WEBHOOK_VERIFY_TOKEN`, `META_WHATSAPP_APP_SECRET`
- For MySQL client/project storage: `CLIENT_STORE_BACKEND`, `MYSQL_URI` (or `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_DATABASE`), `MYSQL_CLIENTS_TABLE`
- For internal actual-cost tracking: `COST_CURRENCY`, `EXOTEL_COST_PER_MINUTE`, `TWILIO_COST_PER_MINUTE`, `AIRTEL_IQ_COST_PER_MINUTE`, `META_WHATSAPP_COST_PER_MINUTE`, `GEMINI_LIVE_INPUT_COST_PER_MINUTE`, `GEMINI_LIVE_OUTPUT_COST_PER_MINUTE`, `GEMINI_TTS_COST_PER_1K_CHARS`, `GEMINI_STRUCTURED_COST_PER_1K_CHARS`
- For shared call state at scale: `SHARED_STATE_BACKEND`, `REDIS_URL`, `REDIS_PREFIX`, `WEBHOOK_IDEMPOTENCY_TTL_SECONDS`, `CALL_STATE_TTL_SECONDS`

## Running the demo

List available clients:

```bash
python -m voice_sales_agent.cli --list-clients
```

Run the demo with the default client:

```bash
python -m voice_sales_agent.cli
```

Run with a specific client:

```bash
python scripts/run_demo.py --client nova_security
```

Run the local browser dashboard:

```bash
python scripts/run_dashboard.py
```

Then open [http://127.0.0.1:8000](http://127.0.0.1:8000).

## Calling a mobile phone

The app can now place an outbound phone call through Twilio or Exotel and run the same Gemini session over the call audio instead of the local microphone and speakers.

To use it:

1. Add the telephony settings in `.env`.

For Exotel:

```bash
TELEPHONY_PROVIDER=exotel
PUBLIC_BASE_URL=https://your-public-url.example.com
EXOTEL_ACCOUNT_SID=your_exotel_account_sid
EXOTEL_API_KEY=your_exotel_api_key
EXOTEL_API_TOKEN=your_exotel_api_token
EXOTEL_CALLER_ID=your_verified_exophone_or_caller_id
EXOTEL_SUBDOMAIN=api.exotel.com
EXOTEL_APP_ID=your_exotel_app_id
```

For Twilio:

```bash
TELEPHONY_PROVIDER=twilio
PUBLIC_BASE_URL=https://your-public-url.example.com
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=your_twilio_auth_token
TWILIO_FROM_NUMBER=+15551234567
```

For Airtel IQ:

```bash
TELEPHONY_PROVIDER=airtel_iq
PUBLIC_BASE_URL=https://your-public-url.example.com
# Option A: single initiate-call endpoint
AIRTEL_IQ_API_URL=https://your-airtel-iq-api.example.com/outbound/call
# Option B: base URL + individual action paths
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
AIRTEL_IQ_REQUEST_TEMPLATE_JSON={"to":"{to_number}","from":"{caller_id}","applicationId":"{application_id}","callbackUrl":"{events_callback_url}","statusCallbackUrl":"{status_callback_url}","cdrUrl":"{cdr_callback_url}","mediaUrl":"{ws_url}"}
```

For Meta WhatsApp (direct API):

```bash
TELEPHONY_PROVIDER=meta_whatsapp
PUBLIC_BASE_URL=https://your-public-url.example.com
META_WHATSAPP_BASE_URL=https://graph.facebook.com
META_WHATSAPP_API_VERSION=v22.0
META_WHATSAPP_ACCESS_TOKEN=your_meta_whatsapp_access_token
META_WHATSAPP_PHONE_NUMBER_ID=your_meta_whatsapp_phone_number_id
META_WHATSAPP_FROM_NUMBER=919999999999
META_WHATSAPP_INITIATE_PATH=/{api_version}/{phone_number_id}/calls
META_WHATSAPP_REQUEST_TEMPLATE_JSON={"messaging_product":"whatsapp","to":"{to_number}","action":"connect","session":{"sdp_type":"{sdp_type}","sdp":"{sdp}"},"callback_url":"{status_callback_url}","biz_opaque_callback_data":"{pending_id}"}
META_WHATSAPP_DEFAULT_SDP_TYPE=offer
META_WHATSAPP_DEFAULT_SDP=replace_with_test_sdp_offer
META_WHATSAPP_WEBHOOK_VERIFY_TOKEN=replace_with_meta_verify_token
META_WHATSAPP_APP_SECRET=replace_with_meta_app_secret
```

For shared call state (recommended for multi-worker deployment):

```bash
SHARED_STATE_BACKEND=redis
REDIS_URL=redis://localhost:6379/0
REDIS_PREFIX=voice_agent
WEBHOOK_IDEMPOTENCY_TTL_SECONDS=900
CALL_STATE_TTL_SECONDS=7200
```

For MySQL client storage:

```bash
CLIENT_STORE_BACKEND=mysql
# Option A: DSN
MYSQL_URI=mysql://user:password@127.0.0.1:3306/voice_agent
# Option B: discrete fields
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=voice_agent
MYSQL_PASSWORD=replace_with_secret
MYSQL_DATABASE=voice_agent
MYSQL_CLIENTS_TABLE=clients
MYSQL_CONNECT_TIMEOUT_SECONDS=5
```

2. Make the FastAPI app reachable from the public internet over HTTPS.

For local development, a tunneling tool such as `ngrok` works well:

```bash
ngrok http 8000
```

For production on AWS Lightsail, follow [`docs/lightsail_deployment.md`](/Users/idriskhan/Documents/new_voice_agent/docs/lightsail_deployment.md).

3. Put the public HTTPS URL (tunnel or domain) into `PUBLIC_BASE_URL`.
4. Start the dashboard with `python scripts/run_dashboard.py`.
5. In the dashboard, enter the mobile number in E.164 format, for example `+15551234567`, then click `Call`.

Notes:

- Twilio will fetch TwiML from `/twilio/voice/outbound/<id>` and then open a bidirectional Media Streams WebSocket to `/twilio/media/<id>`.
- Exotel outbound calls use `/exotel/status/<id>` for status callbacks and now support `/exotel/media/<id>` as the safer per-call media WebSocket bridge.
- Airtel IQ uses `/airtel-iq/events/<id>`, `/airtel-iq/status/<id>`, `/airtel-iq/cdr/<id>`, and `/airtel-iq/media/<id>` for configurable callback and media entry points.
- Meta WhatsApp direct path uses `/meta-whatsapp/status/<id>` and `/meta-whatsapp/webhook` for callbacks and `/api/meta-whatsapp/action` for explicit call actions (`connect`, `pre_accept`, `accept`, `reject`, `terminate`).
- The call session auto-stops when the conversation ends, and closing the WebSocket lets Twilio continue to the trailing `<Hangup/>`.
- The app now supports multiple concurrent pending calls and per-call session isolation.
- Twilio request signatures are not yet validated.
- Exotel setup details are documented in [`docs/exotel_setup.md`](/Users/idriskhan/Documents/new_voice_agent/docs/exotel_setup.md).
- Airtel IQ setup details are documented in [`docs/airtel_iq_setup.md`](/Users/idriskhan/Documents/new_voice_agent/docs/airtel_iq_setup.md).
- Meta WhatsApp setup details are documented in [`docs/meta_whatsapp_setup.md`](/Users/idriskhan/Documents/new_voice_agent/docs/meta_whatsapp_setup.md).
- Shared-state scaling notes are documented in [`docs/redis_scaling.md`](/Users/idriskhan/Documents/new_voice_agent/docs/redis_scaling.md).
- MySQL migration notes are documented in [`docs/mysql_migration.md`](/Users/idriskhan/Documents/new_voice_agent/docs/mysql_migration.md).

The dashboard now includes a file-backed client editor. You can:

- load an existing client
- update display name, industry, offer, voice name, and generation settings
- edit `system_prompt.txt`, `knowledge.md`, `objections.json`, `qualification.json`, and `cta.json`
- save changes directly back to the client's folder without editing files manually

During the call:

- Speak normally into your microphone.
- The app streams microphone audio to Gemini Live.
- Gemini replies with synthesized voice output.
- Status messages and the dashboard show when the system is listening, processing, or speaking.
- Press `Ctrl+C` to end the session and trigger the post-call summary.
- In the dashboard flow, the browser is a control surface. Audio still uses the same machine's local mic and speakers.

## Session outputs

Each session is stored under `sessions/<client_id>/<session_id>/` with:

- `artifacts.json`
- `lead_info.json`
- `summary.json` if summary generation succeeds
- `actual_cost.json` if internal cost tracking is available
- `transcript.md`
- `summary.md`

Structured outputs include:

- transcript
- extracted lead info
- qualification status
- objections raised
- summary
- suggested next action
- internal actual-cost estimate for telephony and Gemini usage

## Client onboarding

Create a new client scaffold:

```bash
python scripts/create_client.py --client acme_finance
```

No redeployment is required for a new customer. The app reads client folders from disk at runtime, so adding a new folder under `clients/` makes that customer available the next time you start the demo.

This creates:

- `config.json`
- `system_prompt.txt`
- `knowledge.md`
- `objections.json`
- `qualification.json`
- `cta.json`

Per-customer model tuning also lives in `config.json`. You can change these without touching Python code:

- `voice.voice_name`
- `live_generation.temperature`
- `live_generation.top_p`
- `live_generation.top_k`
- `live_generation.max_output_tokens`
- `structured_generation.temperature`
- `structured_generation.top_p`
- `structured_generation.top_k`
- `structured_generation.max_output_tokens`

Example:

```json
{
  "live_generation": {
    "temperature": 0.8,
    "top_p": 0.95,
    "top_k": 40,
    "max_output_tokens": 256
  },
  "structured_generation": {
    "temperature": 0.2,
    "top_p": 0.8,
    "top_k": 20,
    "max_output_tokens": 512
  }
}
```

If you prefer not to edit files manually, use the dashboard's `Client Editor` section to update the same values through the browser UI.

Validate one client:

```bash
python scripts/validate_client.py --client acme_health
```

Validate all clients:

```bash
python scripts/validate_client.py
```

## Prompt tuning

To change sales behavior without editing Python:

- Update `prompts/global_system.txt` for app-wide agent rules
- Update `clients/<client_id>/system_prompt.txt` for persona and talk track
- Update `clients/<client_id>/knowledge.md` for approved product knowledge
- Update `clients/<client_id>/objections.json` for objection handling
- Update `clients/<client_id>/qualification.json` for qualification criteria
- Update `clients/<client_id>/cta.json` for call goals and next-step logic

The prompt builder composes all of these into the live session prompt at startup.

## Reliability notes

- The live voice and post-call extraction layers are separated so extraction failures do not break the call loop.
- Post-call summary generation is best-effort and saved with error notes if it fails.
- The local audio transport is isolated so WebRTC or telephony can be added later without rewriting the conversation engine.
- The session layer now emits structured status, turn, summary, and metric events so UI layers can observe the call without duplicating business logic.
- If the exact Gemini Live SDK surface changes, update only `voice_sales_agent/gemini_api.py`.

## Known limitations

- The current session memory is maintained natively by the live Gemini session, while the local app also keeps a lightweight extracted memory for logging and post-call outputs.
- Barge-in handling is best-effort through interruption detection and speaker buffer flushing.
- Voice settings beyond the selected built-in voice may depend on current Gemini SDK support.
- The browser dashboard currently controls a local mic/speaker session; it does not stream browser microphone audio yet.

## Future extension points

- TODO: Add telephony/WebRTC transports while preserving `VoiceSalesSession`
- TODO: Add CRM sync and callback scheduling
- TODO: Extend the dashboard to use browser-side audio capture and playback when needed
