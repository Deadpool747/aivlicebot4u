# Piopiy Gemini Native Simple Checkpoint

This checkpoint switches the Piopiy inbound worker into a minimal Gemini-native test mode:

- `PIOPIY_PIPELINE_MODE=gemini_native_simple`
- `SpeechAgent + GeminiLiveLLMService + GeminiTTSService`
- `PIOPIY_GEMINI_TTS_FACTORY=piopiy.services.google.tts:GeminiTTSService`
- `PIOPIY_GEMINI_TTS_MODEL=gemini-2.5-flash-tts`
- `PIOPIY_GEMINI_TTS_VOICE_ID=Kore`

This mode is intended as a lightweight trial path for Piopiy + Gemini native audio handling.

Rollback target:
