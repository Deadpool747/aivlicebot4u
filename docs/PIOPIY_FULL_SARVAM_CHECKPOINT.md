# Piopiy Full Sarvam Checkpoint

This checkpoint pins the active Piopiy inbound worker to a full Sarvam cascade:

- `PIOPIY_PIPELINE_MODE=cascaded`
- `PIOPIY_LLM_FACTORY=piopiy.services.openai.llm:OpenAILLMService`
- `PIOPIY_LLM_MODEL=sarvam-m`
- `PIOPIY_LLM_BASE_URL=https://api.sarvam.ai/v1`
- `PIOPIY_STT_FACTORY=piopiy.services.sarvam.stt:SarvamSTTService`
- `PIOPIY_TTS_FACTORY=piopiy.services.sarvam.tts:SarvamHttpTTSService`
- top-level providers set to `sarvam`

Worker service:

- `voice-sales-agent.service`
- `piopiy-agent.service`

Use this checkpoint if we need to return to the "full Sarvam" inbound setup later.
