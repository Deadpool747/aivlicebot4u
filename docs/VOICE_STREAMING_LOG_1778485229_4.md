# Voice Streaming Log Report

## Call Reference
- Provider: Tata
- Call ID (`provider_call_sid`): `1778485229.4`
- Internal Stream ID (`pending_id`): `5723d737ce8647cab745f0f675c0c636`
- Source WebSocket: `/tata/media/`
- Log Window (UTC): `2026-05-11 07:40:32` to `2026-05-11 07:41:32`

## Requested Event Flow (Captured)

### 1) Connection Establishment
- WebSocket accepted:
  - `INFO: "WebSocket /tata/media/" [accepted]`
  - Timestamp: `2026-05-11 07:40:32`

### 2) Stream Initialization
- Start event received:
  - `Tata media event: pending_id=<unresolved> event=start`
  - Timestamp: `2026-05-11 07:40:32,520`
- Handshake/session mapping resolved:
  - `Auto-created Tata inbound pending call from media stream: pending_id=5723d737ce8647cab745f0f675c0c636 callSid=1778485229.4`
  - Timestamp: `2026-05-11 07:40:32,537`
- Stream selection/association:
  - `Tata session start selection: pending_id=5723d737ce8647cab745f0f675c0c636 provider_call_sid=1778485229.4 ...`
  - Timestamp: `2026-05-11 07:40:32,538`

### 3) Audio Streaming
- Repeated inbound media frames observed:
  - `Tata media event: pending_id=5723d737ce8647cab745f0f675c0c636 event=media`
- First media frame in this window:
  - Timestamp: `2026-05-11 07:40:35,586`
- Last media frame before stop:
  - Timestamp: `2026-05-11 07:41:32,362`
- Notes:
  - Media payload is base64 encoded audio frames at provider transport layer.
  - Logs confirm sustained media flow for the active stream.

### 4) Stream Termination
- Stop event received:
  - `Tata media event: pending_id=5723d737ce8647cab745f0f675c0c636 event=stop`
  - Timestamp: `2026-05-11 07:41:32,434`
- Stream close handling:
  - `Received Tata stop event, closing media loop pending_id=5723d737ce8647cab745f0f675c0c636`
  - Timestamp: `2026-05-11 07:41:32,434`

### 5) End of Input (mark/clear)
- `mark` event:
  - Not present in this Tata stream log sequence.
- `clear` event:
  - Not present in this Tata stream log sequence.
- Interpretation:
  - For this provider/session, observed flow is `start -> media -> stop`.
  - `mark`/`clear` semantics were not emitted for this call stream.

## Additional Observations
- Inbound webhook successfully matched to the same pending stream:
  - `Matched Tata inbound webhook to pending_id=5723d737ce8647cab745f0f675c0c636 ... call_id=1778485229.4`
  - Timestamp: `2026-05-11 07:40:33,579`
- Session finalized after stop event as expected.

