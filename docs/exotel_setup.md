# Exotel API Setup

This project already includes an Exotel outbound calling path. To create and wire it successfully, you need four things aligned:

- Exotel account credentials
- an ExoML app inside Exotel
- a public HTTPS base URL for this FastAPI app
- the local app environment variables

## 1. Environment variables

Add these to `.env`:

```bash
TELEPHONY_PROVIDER=exotel
PUBLIC_BASE_URL=https://your-public-host.example.com

EXOTEL_ACCOUNT_SID=your_exotel_account_sid
EXOTEL_API_KEY=your_exotel_api_key
EXOTEL_API_TOKEN=your_exotel_api_token
EXOTEL_CALLER_ID=your_verified_exophone_or_caller_id
EXOTEL_SUBDOMAIN=api.exotel.com
EXOTEL_APP_ID=your_exotel_app_id
```

What each value is used for:

- `TELEPHONY_PROVIDER`: chooses the Exotel call flow in the dashboard API.
- `PUBLIC_BASE_URL`: the public HTTPS base URL Exotel can reach for callbacks and media.
- `EXOTEL_ACCOUNT_SID`: your Exotel account identifier used in REST URLs.
- `EXOTEL_API_KEY`: the API username for Basic auth.
- `EXOTEL_API_TOKEN`: the API password/token for Basic auth.
- `EXOTEL_CALLER_ID`: the Exotel number or verified caller ID shown on outbound calls.
- `EXOTEL_SUBDOMAIN`: the Exotel API host used by your account.
- `EXOTEL_APP_ID`: the ExoML app ID used in the outbound connect request.

## 2. What the app sends to Exotel

When you click `Call` in the dashboard, the app creates an outbound request to:

```text
https://<EXOTEL_SUBDOMAIN>/v1/Accounts/<EXOTEL_ACCOUNT_SID>/Calls/connect
```

With these form fields:

```text
From=<customer_phone_number>
CallerId=<EXOTEL_CALLER_ID>
CallType=trans
Url=http://my.exotel.in/exoml/start/<EXOTEL_APP_ID>
StatusCallback=<PUBLIC_BASE_URL>/exotel/status/<pending_id>
```

That means your Exotel app must be prepared to start from the ExoML app identified by `EXOTEL_APP_ID`.

## 3. Public endpoints Exotel needs

This app exposes these Exotel-facing routes:

- `POST /exotel/status/{pending_id}`
- `GET /exotel/ws-url`
- `GET /exotel/ws-url/{pending_id}`
- `WS /exotel/media`
- `WS /exotel/media/{pending_id}`

What they do:

- `/exotel/status/{pending_id}` receives Exotel call status callbacks.
- `/exotel/ws-url/{pending_id}` returns the safest per-call WebSocket URL for a specific pending call.
- `/exotel/ws-url` returns a call-specific URL when there is exactly one pending Exotel call, otherwise it falls back or asks for a specific `pending_id`.
- `/exotel/media/{pending_id}` accepts a bidirectional media stream for one known pending call.
- `/exotel/media` remains as a backward-compatible fallback path.

For a local machine, expose port `8000` through a public HTTPS tunnel and set that tunnel URL as `PUBLIC_BASE_URL`.

## 4. ExoML app requirements

Inside Exotel, create or update an ExoML app that:

- starts from the `EXOTEL_APP_ID` you configured
- opens a bidirectional audio stream to this app's WebSocket
- preferably uses the WebSocket URL returned by `GET /exotel/ws-url/{pending_id}` when you can pass `pending_id`
- otherwise uses `GET /exotel/ws-url`, which now returns a call-specific URL whenever there is exactly one pending Exotel call
- keeps the call active while the WebSocket stream is connected

Recommended production setup:

- resolve a call-specific WebSocket URL such as `wss://<public-host>/exotel/media/<pending_id>`
- avoid using the generic `/exotel/media` path unless you intentionally want the fallback behavior

The app code expects Exotel media WebSocket events in this shape:

### Start event

```json
{
  "event": "start",
  "stream_sid": "stream-id",
  "call_sid": "call-id",
  "start": {
    "stream_sid": "stream-id",
    "call_sid": "call-id"
  }
}
```

### Media event

```json
{
  "event": "media",
  "media": {
    "payload": "<base64-encoded-8kHz-pcm-audio>"
  }
}
```

### Stop event

```json
{
  "event": "stop"
}
```

The app sends audio back to Exotel in this shape:

```json
{
  "event": "media",
  "sequence_number": 1,
  "stream_sid": "stream-id",
  "media": {
    "track": "outbound",
    "chunk": 1,
    "timestamp": 20,
    "payload": "<base64-encoded-8kHz-pcm-audio>"
  }
}
```

## 5. Audio assumptions

The current bridge assumes:

- inbound Exotel audio is raw PCM at `8 kHz`
- the app resamples inbound audio to `16 kHz` for Gemini input
- Gemini output audio is resampled from `24 kHz` down to `8 kHz` before sending back to Exotel

If your Exotel stream uses a different codec or sample rate, the media bridge will need to be adjusted in `voice_sales_agent/telephony.py`.

## 6. Local run flow

1. Fill in `.env` with the Exotel values above.
2. Start a public HTTPS tunnel to port `8000`.
3. Set `PUBLIC_BASE_URL` to that tunnel URL.
4. Run `python scripts/run_dashboard.py`.
5. Optionally verify credentials with `python scripts/check_exotel_auth.py`.
6. Open the dashboard and place a call.

## 7. Quick validation checklist

- `TELEPHONY_PROVIDER=exotel`
- `PUBLIC_BASE_URL` is public and HTTPS
- Exotel can reach `/exotel/status/{pending_id}`
- Exotel media can connect to `wss://<public-host>/exotel/media/<pending_id>` for the current call
- `EXOTEL_APP_ID` matches the ExoML app you created
- `EXOTEL_CALLER_ID` is valid in your Exotel account
- `python scripts/check_exotel_auth.py` returns `AUTH_OK`

## 8. Relevant code locations

- `voice_sales_agent/config.py`
- `voice_sales_agent/telephony.py`
- `voice_sales_agent/web_app.py`
- `scripts/check_exotel_auth.py`
