# Piopiy Direct Gemini Bridge

This mode keeps Piopiy only as the telephony transport layer.

Architecture:

- Piopiy answers the call and opens `/piopiy/stream/{pending_id}`
- our FastAPI app accepts the websocket
- our backend connects directly to Gemini Live
- caller audio is forwarded to Gemini Live as raw PCM
- Gemini Live audio is written back to Piopiy as raw PCM

## Enable it

Set:

```env
PIOPIY_USE_WEB_BRIDGE=true
PIOPIY_STREAM_RUNTIME=direct_gemini_bridge
```

Optional:

```env
PIOPIY_DIRECT_GEMINI_SKIP_OPENING=false
```

## Notes

- This bypasses the Piopiy worker SDK conversation pipeline for streamed calls.
- Piopiy still remains the phone transport and webhook provider.
- The existing `session_controller` path remains available as the default fallback.
