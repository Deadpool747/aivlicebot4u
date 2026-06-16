"""Environment and runtime configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

from .constants import PROJECT_ROOT


@dataclass(slots=True)
class AppSettings:
    speech_provider: Literal["gemini", "sarvam"]
    gemini_api_key: str
    live_model: str
    structured_model: str
    tts_model: str
    agent_response_mode: Literal["live_audio", "text_tts"]
    default_client_id: str
    session_output_dir: Path
    call_outcomes_db_path: Path
    log_level: str
    processing_ambience_enabled: bool
    processing_ambience_gain: float
    public_base_url: str | None
    telephony_provider: Literal["twilio", "exotel", "airtel_iq", "meta_whatsapp", "tata", "piopiy"]
    twilio_account_sid: str | None
    twilio_auth_token: str | None
    twilio_from_number: str | None
    twilio_inbound_client_id: str | None
    twilio_inbound_project_id: str | None
    exotel_account_sid: str | None
    exotel_api_key: str | None
    exotel_api_token: str | None
    exotel_caller_id: str | None
    exotel_subdomain: str | None
    exotel_app_id: str | None
    exotel_echo_test: bool
    exotel_interruption_grace_seconds: float
    exotel_barge_in_debounce_seconds: float
    exotel_short_response_commit_seconds: float
    exotel_partial_commit_silence_seconds: float
    exotel_short_reply_partial_commit_silence_seconds: float
    exotel_min_partial_commit_characters: int
    exotel_enable_partial_commit_watchdog: bool
    exotel_use_explicit_vad: bool
    exotel_enable_low_confidence_reprompt: bool
    exotel_low_confidence_reprompt_silence_seconds: float
    exotel_low_confidence_audio_threshold: float
    airtel_iq_base_url: str | None
    airtel_iq_api_url: str | None
    airtel_iq_api_key: str | None
    airtel_iq_api_secret: str | None
    airtel_iq_application_id: str | None
    airtel_iq_caller_id: str | None
    airtel_iq_initiate_path: str
    airtel_iq_play_audio_path: str
    airtel_iq_collect_input_path: str
    airtel_iq_hangup_path: str
    airtel_iq_headers_json: str | None
    airtel_iq_request_template_json: str | None
    tata_click2call_api_url: str | None
    tata_api_key: str | None
    tata_api_token: str | None
    tata_agent_number: str | None
    tata_caller_id: str | None
    tata_async: bool
    tata_headers_json: str | None
    tata_request_template_json: str | None
    meta_whatsapp_base_url: str | None
    meta_whatsapp_api_version: str
    meta_whatsapp_access_token: str | None
    meta_whatsapp_phone_number_id: str | None
    meta_whatsapp_from_number: str | None
    meta_whatsapp_initiate_path: str
    meta_whatsapp_messages_path: str
    meta_whatsapp_request_template_json: str | None
    meta_whatsapp_default_sdp_type: str
    meta_whatsapp_default_sdp: str | None
    meta_whatsapp_webhook_verify_token: str | None
    meta_whatsapp_app_secret: str | None
    meta_whatsapp_outbound_source_rate: int
    meta_whatsapp_outbound_preroll_ms: int
    piopiy_api_token: str | None
    piopiy_agent_id: str | None
    piopiy_caller_id: str | None
    piopiy_app_id: str | None
    cost_currency: str
    exotel_cost_per_minute: float
    twilio_cost_per_minute: float
    airtel_iq_cost_per_minute: float
    tata_cost_per_minute: float
    piopiy_cost_per_minute: float
    meta_whatsapp_cost_per_minute: float
    gemini_live_input_cost_per_minute: float
    gemini_live_output_cost_per_minute: float
    gemini_tts_cost_per_1k_chars: float
    gemini_structured_cost_per_1k_chars: float
    faster_stt_enabled: bool
    faster_stt_model: str
    faster_stt_device: str
    faster_stt_compute_type: str
    faster_stt_beam_size: int
    parallel_stt_enabled: bool
    parallel_stt_segment_seconds: float
    parallel_stt_finalize_timeout_seconds: float
    recording_stt_enabled: bool
    recording_stt_chunk_seconds: int
    recording_llm_extraction_enabled: bool
    client_store_backend: Literal["auto", "file", "mysql", "mongo"]
    mysql_uri: str | None
    mysql_host: str | None
    mysql_port: int
    mysql_user: str | None
    mysql_password: str | None
    mysql_database: str
    mysql_clients_table: str
    mysql_connect_timeout_seconds: int
    shared_state_backend: Literal["auto", "memory", "redis"]
    redis_url: str | None
    redis_prefix: str
    webhook_idempotency_ttl_seconds: int
    call_state_ttl_seconds: int
    telephony_max_concurrent_sessions: int
    smartflo_enable_dsp: bool
    smartflo_frame_ms: int
    smartflo_target_rms_dbfs: float
    smartflo_max_queue_ms: int
    smartflo_highpass_hz: int
    smartflo_lowpass_hz: int
    smartflo_enable_noise_gate: bool
    smartflo_noise_gate_threshold: int
    smartflo_latency_log_interval_seconds: float
    sarvam_api_key: str | None
    sarvam_base_url: str
    sarvam_tts_model: str
    sarvam_tts_speaker: str
    sarvam_tts_target_language_code: str
    sarvam_stt_model: str
    sarvam_stt_mode: str
    sarvam_stt_language_code: str
    sarvam_chat_model: str


def load_settings() -> AppSettings:
    """Load application settings from the environment."""
    load_dotenv(PROJECT_ROOT / ".env")

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Missing GEMINI_API_KEY. Add it to your environment or .env file.")

    return AppSettings(
        speech_provider=os.getenv("SPEECH_PROVIDER", "gemini").strip().lower() or "gemini",
        gemini_api_key=api_key,
        live_model=os.getenv("GEMINI_LIVE_MODEL", "gemini-2.5-flash-native-audio-preview-12-2025"),
        structured_model=os.getenv("GEMINI_STRUCTURED_MODEL", "gemini-2.5-flash"),
        tts_model=os.getenv("GEMINI_TTS_MODEL", "gemini-2.5-flash-preview-tts"),
        agent_response_mode=os.getenv("AGENT_RESPONSE_MODE", "live_audio").strip().lower() or "live_audio",
        default_client_id=os.getenv("DEFAULT_CLIENT_ID", "acme_health"),
        session_output_dir=Path(os.getenv("SESSION_OUTPUT_DIR", PROJECT_ROOT / "sessions")).resolve(),
        call_outcomes_db_path=Path(
            os.getenv("CALL_OUTCOMES_DB_PATH", Path(os.getenv("SESSION_OUTPUT_DIR", PROJECT_ROOT / "sessions")) / "call_outcomes.db")
        ).resolve(),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        processing_ambience_enabled=(
            os.getenv("PROCESSING_AMBIENCE_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
            if os.getenv("PROCESSING_AMBIENCE_ENABLED") is not None
            else True
        ),
        processing_ambience_gain=float(os.getenv("PROCESSING_AMBIENCE_GAIN", "0.08").strip() or "0.08"),
        public_base_url=(os.getenv("PUBLIC_BASE_URL", "").strip() or None),
        telephony_provider=os.getenv("TELEPHONY_PROVIDER", "piopiy").strip().lower() or "piopiy",
        twilio_account_sid=(os.getenv("TWILIO_ACCOUNT_SID", "").strip() or None),
        twilio_auth_token=(os.getenv("TWILIO_AUTH_TOKEN", "").strip() or None),
        twilio_from_number=(os.getenv("TWILIO_FROM_NUMBER", "").strip() or None),
        twilio_inbound_client_id=(os.getenv("TWILIO_INBOUND_CLIENT_ID", "").strip() or None),
        twilio_inbound_project_id=(os.getenv("TWILIO_INBOUND_PROJECT_ID", "").strip() or None),
        exotel_account_sid=(os.getenv("EXOTEL_ACCOUNT_SID", "").strip() or None),
        exotel_api_key=(os.getenv("EXOTEL_API_KEY", "").strip() or None),
        exotel_api_token=(os.getenv("EXOTEL_API_TOKEN", "").strip() or None),
        exotel_caller_id=(os.getenv("EXOTEL_CALLER_ID", "").strip() or None),
        exotel_subdomain=(os.getenv("EXOTEL_SUBDOMAIN", "").strip() or None),
        exotel_app_id=(os.getenv("EXOTEL_APP_ID", "").strip() or None),
        exotel_echo_test=(os.getenv("EXOTEL_ECHO_TEST", "").strip().lower() in {"1", "true", "yes", "on"}),
        exotel_interruption_grace_seconds=float(
            os.getenv("EXOTEL_INTERRUPTION_GRACE_SECONDS", "0.16").strip() or "0.16"
        ),
        exotel_barge_in_debounce_seconds=float(
            os.getenv("EXOTEL_BARGE_IN_DEBOUNCE_SECONDS", "0.01").strip() or "0.01"
        ),
        exotel_short_response_commit_seconds=float(
            os.getenv("EXOTEL_SHORT_RESPONSE_COMMIT_SECONDS", "0.12").strip() or "0.12"
        ),
        exotel_partial_commit_silence_seconds=float(
            os.getenv("EXOTEL_PARTIAL_COMMIT_SILENCE_SECONDS", "0.35").strip() or "0.35"
        ),
        exotel_short_reply_partial_commit_silence_seconds=float(
            os.getenv("EXOTEL_SHORT_REPLY_PARTIAL_COMMIT_SILENCE_SECONDS", "0.12").strip() or "0.12"
        ),
        exotel_min_partial_commit_characters=int(
            os.getenv("EXOTEL_MIN_PARTIAL_COMMIT_CHARACTERS", "2").strip() or "2"
        ),
        exotel_enable_partial_commit_watchdog=(
            os.getenv("EXOTEL_ENABLE_PARTIAL_COMMIT_WATCHDOG", "").strip().lower() in {"1", "true", "yes", "on"}
            if os.getenv("EXOTEL_ENABLE_PARTIAL_COMMIT_WATCHDOG") is not None
            else True
        ),
        exotel_use_explicit_vad=(
            os.getenv("EXOTEL_USE_EXPLICIT_VAD", "").strip().lower() in {"1", "true", "yes", "on"}
            if os.getenv("EXOTEL_USE_EXPLICIT_VAD") is not None
            else False
        ),
        exotel_enable_low_confidence_reprompt=(
            os.getenv("EXOTEL_ENABLE_LOW_CONFIDENCE_REPROMPT", "").strip().lower() in {"1", "true", "yes", "on"}
            if os.getenv("EXOTEL_ENABLE_LOW_CONFIDENCE_REPROMPT") is not None
            else True
        ),
        exotel_low_confidence_reprompt_silence_seconds=float(
            os.getenv("EXOTEL_LOW_CONFIDENCE_REPROMPT_SILENCE_SECONDS", "0.7").strip() or "0.7"
        ),
        exotel_low_confidence_audio_threshold=float(
            os.getenv("EXOTEL_LOW_CONFIDENCE_AUDIO_THRESHOLD", "120").strip() or "120"
        ),
        airtel_iq_base_url=(os.getenv("AIRTEL_IQ_BASE_URL", "").strip() or None),
        airtel_iq_api_url=(os.getenv("AIRTEL_IQ_API_URL", "").strip() or None),
        airtel_iq_api_key=(os.getenv("AIRTEL_IQ_API_KEY", "").strip() or None),
        airtel_iq_api_secret=(os.getenv("AIRTEL_IQ_API_SECRET", "").strip() or None),
        airtel_iq_application_id=(os.getenv("AIRTEL_IQ_APPLICATION_ID", "").strip() or None),
        airtel_iq_caller_id=(os.getenv("AIRTEL_IQ_CALLER_ID", "").strip() or None),
        airtel_iq_initiate_path=os.getenv("AIRTEL_IQ_INITIATE_PATH", "/voice/call/initiate").strip() or "/voice/call/initiate",
        airtel_iq_play_audio_path=os.getenv("AIRTEL_IQ_PLAY_AUDIO_PATH", "/voice/call/playAudio").strip() or "/voice/call/playAudio",
        airtel_iq_collect_input_path=os.getenv("AIRTEL_IQ_COLLECT_INPUT_PATH", "/voice/call/collectInput").strip() or "/voice/call/collectInput",
        airtel_iq_hangup_path=os.getenv("AIRTEL_IQ_HANGUP_PATH", "/voice/call/hangup").strip() or "/voice/call/hangup",
        airtel_iq_headers_json=(os.getenv("AIRTEL_IQ_HEADERS_JSON", "").strip() or None),
        airtel_iq_request_template_json=(os.getenv("AIRTEL_IQ_REQUEST_TEMPLATE_JSON", "").strip() or None),
        tata_click2call_api_url=(os.getenv("TATA_CLICK2CALL_API_URL", "").strip() or None),
        tata_api_key=(os.getenv("TATA_API_KEY", "").strip() or None),
        tata_api_token=(os.getenv("TATA_API_TOKEN", "").strip() or None),
        tata_agent_number=(os.getenv("TATA_AGENT_NUMBER", "").strip() or None),
        tata_caller_id=(os.getenv("TATA_CALLER_ID", "").strip() or None),
        tata_async=(os.getenv("TATA_ASYNC", "1").strip().lower() in {"1", "true", "yes", "on"}),
        tata_headers_json=(os.getenv("TATA_HEADERS_JSON", "").strip() or None),
        tata_request_template_json=(os.getenv("TATA_REQUEST_TEMPLATE_JSON", "").strip() or None),
        meta_whatsapp_base_url=(os.getenv("META_WHATSAPP_BASE_URL", "").strip() or None),
        meta_whatsapp_api_version=(os.getenv("META_WHATSAPP_API_VERSION", "v22.0").strip() or "v22.0"),
        meta_whatsapp_access_token=(os.getenv("META_WHATSAPP_ACCESS_TOKEN", "").strip() or None),
        meta_whatsapp_phone_number_id=(os.getenv("META_WHATSAPP_PHONE_NUMBER_ID", "").strip() or None),
        meta_whatsapp_from_number=(os.getenv("META_WHATSAPP_FROM_NUMBER", "").strip() or None),
        meta_whatsapp_initiate_path=(
            os.getenv("META_WHATSAPP_INITIATE_PATH", "/{api_version}/{phone_number_id}/calls")
            .strip()
            or "/{api_version}/{phone_number_id}/calls"
        ),
        meta_whatsapp_messages_path=(
            os.getenv("META_WHATSAPP_MESSAGES_PATH", "/{api_version}/{phone_number_id}/messages")
            .strip()
            or "/{api_version}/{phone_number_id}/messages"
        ),
        meta_whatsapp_request_template_json=(
            os.getenv("META_WHATSAPP_REQUEST_TEMPLATE_JSON", "").strip() or None
        ),
        meta_whatsapp_default_sdp_type=(os.getenv("META_WHATSAPP_DEFAULT_SDP_TYPE", "offer").strip() or "offer"),
        meta_whatsapp_default_sdp=(os.getenv("META_WHATSAPP_DEFAULT_SDP", "").strip() or None),
        meta_whatsapp_webhook_verify_token=(os.getenv("META_WHATSAPP_WEBHOOK_VERIFY_TOKEN", "").strip() or None),
        meta_whatsapp_app_secret=(os.getenv("META_WHATSAPP_APP_SECRET", "").strip() or None),
        meta_whatsapp_outbound_source_rate=int(
            os.getenv("META_WHATSAPP_OUTBOUND_SOURCE_RATE", "24000").strip() or "24000"
        ),
        meta_whatsapp_outbound_preroll_ms=int(
            os.getenv("META_WHATSAPP_OUTBOUND_PREROLL_MS", "140").strip() or "140"
        ),
        piopiy_api_token=(os.getenv("PIOPIY_API_TOKEN", "").strip() or None),
        piopiy_agent_id=(os.getenv("PIOPIY_AGENT_ID", "").strip() or None),
        piopiy_caller_id=(os.getenv("PIOPIY_CALLER_ID", "").strip() or None),
        piopiy_app_id=(os.getenv("PIOPIY_APP_ID", "").strip() or None),
        cost_currency=os.getenv("COST_CURRENCY", "INR").strip().upper() or "INR",
        exotel_cost_per_minute=float(os.getenv("EXOTEL_COST_PER_MINUTE", "0").strip() or "0"),
        twilio_cost_per_minute=float(os.getenv("TWILIO_COST_PER_MINUTE", "0").strip() or "0"),
        airtel_iq_cost_per_minute=float(os.getenv("AIRTEL_IQ_COST_PER_MINUTE", "0").strip() or "0"),
        tata_cost_per_minute=float(os.getenv("TATA_COST_PER_MINUTE", "0").strip() or "0"),
        piopiy_cost_per_minute=float(os.getenv("PIOPIY_COST_PER_MINUTE", "0").strip() or "0"),
        meta_whatsapp_cost_per_minute=float(os.getenv("META_WHATSAPP_COST_PER_MINUTE", "0").strip() or "0"),
        gemini_live_input_cost_per_minute=float(os.getenv("GEMINI_LIVE_INPUT_COST_PER_MINUTE", "0").strip() or "0"),
        gemini_live_output_cost_per_minute=float(os.getenv("GEMINI_LIVE_OUTPUT_COST_PER_MINUTE", "0").strip() or "0"),
        gemini_tts_cost_per_1k_chars=float(os.getenv("GEMINI_TTS_COST_PER_1K_CHARS", "0").strip() or "0"),
        gemini_structured_cost_per_1k_chars=float(os.getenv("GEMINI_STRUCTURED_COST_PER_1K_CHARS", "0").strip() or "0"),
        faster_stt_enabled=(os.getenv("FASTER_STT_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}),
        faster_stt_model=os.getenv("FASTER_STT_MODEL", "small").strip() or "small",
        faster_stt_device=os.getenv("FASTER_STT_DEVICE", "cpu").strip() or "cpu",
        faster_stt_compute_type=os.getenv("FASTER_STT_COMPUTE_TYPE", "int8").strip() or "int8",
        faster_stt_beam_size=int(os.getenv("FASTER_STT_BEAM_SIZE", "5").strip() or "5"),
        parallel_stt_enabled=(os.getenv("PARALLEL_STT_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}),
        parallel_stt_segment_seconds=float(os.getenv("PARALLEL_STT_SEGMENT_SECONDS", "8").strip() or "8"),
        parallel_stt_finalize_timeout_seconds=float(
            os.getenv("PARALLEL_STT_FINALIZE_TIMEOUT_SECONDS", "12").strip() or "12"
        ),
        recording_stt_enabled=(os.getenv("RECORDING_STT_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}),
        recording_stt_chunk_seconds=int(os.getenv("RECORDING_STT_CHUNK_SECONDS", "8").strip() or "8"),
        recording_llm_extraction_enabled=(
            os.getenv("RECORDING_LLM_EXTRACTION_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}
        ),
        client_store_backend=(os.getenv("CLIENT_STORE_BACKEND", "auto").strip().lower() or "auto"),
        mysql_uri=(os.getenv("MYSQL_URI", "").strip() or None),
        mysql_host=(os.getenv("MYSQL_HOST", "").strip() or None),
        mysql_port=int(os.getenv("MYSQL_PORT", "3306").strip() or "3306"),
        mysql_user=(os.getenv("MYSQL_USER", "").strip() or None),
        mysql_password=(os.getenv("MYSQL_PASSWORD", "").strip() or None),
        mysql_database=os.getenv("MYSQL_DATABASE", "voice_agent").strip() or "voice_agent",
        mysql_clients_table=os.getenv("MYSQL_CLIENTS_TABLE", "clients").strip() or "clients",
        mysql_connect_timeout_seconds=int(
            os.getenv("MYSQL_CONNECT_TIMEOUT_SECONDS", "5").strip() or "5"
        ),
        shared_state_backend=(os.getenv("SHARED_STATE_BACKEND", "auto").strip().lower() or "auto"),
        redis_url=(os.getenv("REDIS_URL", "").strip() or None),
        redis_prefix=(os.getenv("REDIS_PREFIX", "voice_agent").strip() or "voice_agent"),
        webhook_idempotency_ttl_seconds=int(os.getenv("WEBHOOK_IDEMPOTENCY_TTL_SECONDS", "900").strip() or "900"),
        call_state_ttl_seconds=int(os.getenv("CALL_STATE_TTL_SECONDS", "7200").strip() or "7200"),
        telephony_max_concurrent_sessions=int(
            os.getenv("TELEPHONY_MAX_CONCURRENT_SESSIONS", "10").strip() or "10"
        ),
        smartflo_enable_dsp=(os.getenv("SMARTFLO_ENABLE_DSP", "1").strip().lower() in {"1", "true", "yes", "on"}),
        smartflo_frame_ms=int(os.getenv("SMARTFLO_FRAME_MS", "20").strip() or "20"),
        smartflo_target_rms_dbfs=float(os.getenv("SMARTFLO_TARGET_RMS_DBFS", "-19").strip() or "-19"),
        smartflo_max_queue_ms=int(os.getenv("SMARTFLO_MAX_QUEUE_MS", "120").strip() or "120"),
        smartflo_highpass_hz=int(os.getenv("SMARTFLO_HIGHPASS_HZ", "100").strip() or "100"),
        smartflo_lowpass_hz=int(os.getenv("SMARTFLO_LOWPASS_HZ", "3400").strip() or "3400"),
        smartflo_enable_noise_gate=(
            os.getenv("SMARTFLO_ENABLE_NOISE_GATE", "1").strip().lower() in {"1", "true", "yes", "on"}
        ),
        smartflo_noise_gate_threshold=int(os.getenv("SMARTFLO_NOISE_GATE_THRESHOLD", "180").strip() or "180"),
        smartflo_latency_log_interval_seconds=float(
            os.getenv("SMARTFLO_LATENCY_LOG_INTERVAL_SECONDS", "5").strip() or "5"
        ),
        sarvam_api_key=(os.getenv("SARVAM_API_KEY", "").strip() or None),
        sarvam_base_url=(os.getenv("SARVAM_BASE_URL", "https://api.sarvam.ai").strip() or "https://api.sarvam.ai"),
        sarvam_tts_model=(os.getenv("SARVAM_TTS_MODEL", "bulbul:v3").strip() or "bulbul:v3"),
        sarvam_tts_speaker=(os.getenv("SARVAM_TTS_SPEAKER", "shubh").strip() or "shubh"),
        sarvam_tts_target_language_code=(os.getenv("SARVAM_TTS_TARGET_LANGUAGE_CODE", "en-IN").strip() or "en-IN"),
        sarvam_stt_model=(os.getenv("SARVAM_STT_MODEL", "saaras:v3").strip() or "saaras:v3"),
        sarvam_stt_mode=(os.getenv("SARVAM_STT_MODE", "transcribe").strip() or "transcribe"),
        sarvam_stt_language_code=(os.getenv("SARVAM_STT_LANGUAGE_CODE", "unknown").strip() or "unknown"),
        sarvam_chat_model=(os.getenv("SARVAM_CHAT_MODEL", "sarvam-30b").strip() or "sarvam-30b"),
    )
