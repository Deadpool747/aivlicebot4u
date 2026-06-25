"""High-level session orchestration for the local voice demo."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import uuid
import wave
from array import array
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from time import monotonic
import time

from google.genai.errors import APIError
from rich.console import Console
from rich.table import Table
from websockets.exceptions import ConnectionClosed
from typing import Any

from .audio import LocalAudioIO
from .call_outcomes_store import SqliteCallOutcomeStore
from .clients import load_client
from .config import AppSettings
from .cost_tracking import SessionCostTracker
from .events import SessionEvent, SessionEventHandler
from .extraction import build_extraction_prompt, build_recording_details_prompt, merge_memory
from .gemini_api import LiveEvent
from .crm_integrations import trigger_post_call_integrations
from .language import detect_conversation_language, infer_opening_language
from .memory import update_memory_from_user_text
from .models import CriticalCallDetails, PostCallSummary, SessionArtifacts, TranscriptTurn
from .provider_runtime import build_provider_runtime
from .prompt_builder import PromptBuilder
from .transcripts import SessionLogger

logger = logging.getLogger(__name__)

AGENT_PLAYBACK_COOLDOWN_SECONDS = 0.025 # 0.25
SILENCE_FOLLOW_UP_SECONDS = 9999.0
INTERRUPTION_GRACE_SECONDS = 0.3
MIN_BARGE_IN_CHARACTERS = 2
BARGE_IN_DEBOUNCE_SECONDS = 0.025
NATIVE_PROMPT_PREROLL_SECONDS = 0.025
AUTO_STOP_GRACE_SECONDS = 0.2
SHORT_RESPONSE_COMMIT_SECONDS = 0.22
EXOTEL_INTERRUPTION_GRACE_SECONDS = 0.16
EXOTEL_BARGE_IN_DEBOUNCE_SECONDS = 0.01
EXOTEL_SHORT_RESPONSE_COMMIT_SECONDS = 0.12
EXOTEL_PARTIAL_COMMIT_SILENCE_SECONDS = 0.35
EXOTEL_MIN_PARTIAL_COMMIT_CHARACTERS = 2
EXOTEL_FIRST_REPLY_GREETING_HOLD_SECONDS = 0.35
FAST_TURN_SHORT_RESPONSE_COMMIT_SECONDS = 0.05
FAST_TURN_PARTIAL_COMMIT_SILENCE_SECONDS = 0.16
FAST_TURN_SHORT_REPLY_PARTIAL_COMMIT_SILENCE_SECONDS = 0.08
GENERIC_BINARY_CONFIRMATION_AUDIO_THRESHOLD = 90.0
GENERIC_BINARY_CONFIRMATION_SILENCE_SECONDS = 0.45
# Browser demos vary a lot by mic quality and speaker volume, so keep the
# turn-end detector forgiving enough to avoid dropped or hanging turns.
BROWSER_EXPLICIT_VAD_AUDIO_THRESHOLD = 90.0
BROWSER_EXPLICIT_VAD_END_SECONDS = 0.7
TWILIO_SHORT_RESPONSE_COMMIT_SECONDS = 0.18
TWILIO_PARTIAL_COMMIT_SILENCE_SECONDS = 0.14
TWILIO_SHORT_REPLY_PARTIAL_COMMIT_SILENCE_SECONDS = 0.08
APPOINTMENT_DETERMINISTIC_FOLLOWUP_DELAY_SECONDS = 0.18
DEFAULT_DETERMINISTIC_FOLLOWUP_DELAY_SECONDS = 1.10
GUEST_SILENCE_FOLLOW_UP_SECONDS = 6.0
MAX_LIVE_RECONNECT_ATTEMPTS = 1
POST_CALL_SUMMARY_TIMEOUT_SECONDS = 12.0
POST_CALL_SUMMARY_MAX_ATTEMPTS = 3
POST_CALL_SUMMARY_RETRY_BACKOFF_SECONDS = 0.8
PARALLEL_STT_MIN_SEGMENT_SECONDS = 2.0
RECORDING_DETAILS_EXTRACTION_TIMEOUT_SECONDS = 14.0
RECORDING_DETAILS_EXTRACTION_MAX_ATTEMPTS = 3
RECORDING_DETAILS_RETRY_BACKOFF_SECONDS = 0.9
POST_ENRICHMENT_STAGE_TIMEOUT_SECONDS = 45.0


class VoiceSalesSession:
    """Coordinate client config, live voice loop, transcript logging, and summaries."""

    def __init__(
        self,
        settings: AppSettings,
        client_id: str,
        project_id: str | None = None,
        customer_name: str | None = None,
        contact_details: dict[str, str] | None = None,
        console: Console | None = None,
        event_handler: SessionEventHandler | None = None,
        audio: Any | None = None,
        telephony_context: dict[str, Any] | None = None,
        defer_initial_prompt: bool = False,
        session_id: str | None = None,
    ) -> None:
        self.settings = settings
        self.client = load_client(client_id, project_id=project_id)
        self.project = self.client.active_project
        self.customer_name = (customer_name or "Salman Shaikh").strip() or "Salman Shaikh"
        self.contact_details = {
            str(key): str(value).strip()
            for key, value in (contact_details or {}).items()
            if str(value or "").strip()
        }
        self.opening_language = (
            self._guest_demo_opening_language()
            or self.client.config.default_opening_language
            or infer_opening_language(self.customer_name)
        )
        self.current_language = self.opening_language
        self.console = console or Console()
        self.event_handler = event_handler
        self.prompt_builder = PromptBuilder()
        self.session_logger = SessionLogger(settings.session_output_dir)
        self.call_outcome_store = SqliteCallOutcomeStore(settings.call_outcomes_db_path)
        self.audio = audio or LocalAudioIO()
        project_runtime = self.project.runtime if self.project is not None else None
        runtime_api_key = (
            (project_runtime.gemini_api_key if project_runtime else None)
            or (os.getenv(project_runtime.gemini_api_key_env or "") if project_runtime and project_runtime.gemini_api_key_env else None)
            or settings.gemini_api_key
        )
        runtime_structured_api_key = os.getenv("GEMINI_API_KEY_STRUCTURED", "").strip() or runtime_api_key
        runtime_live_model = (project_runtime.live_model if project_runtime else None) or settings.live_model
        runtime_structured_model = (project_runtime.structured_model if project_runtime else None) or settings.structured_model
        runtime_structured_fallback_model = os.getenv("GEMINI_STRUCTURED_FALLBACK_MODEL", "").strip()
        runtime_tts_model = (project_runtime.tts_model if project_runtime else None) or settings.tts_model
        runtime = build_provider_runtime(
            settings,
            runtime_api_key=runtime_api_key,
            runtime_structured_api_key=runtime_structured_api_key,
            runtime_live_model=runtime_live_model,
            runtime_structured_model=runtime_structured_model,
            runtime_structured_fallback_model=(runtime_structured_fallback_model or None),
            runtime_tts_model=runtime_tts_model,
        )
        self.live = runtime.live
        self.structured = runtime.structured
        self.structured_fallback = runtime.structured_fallback
        self.speech = runtime.speech
        self.cost_tracker = SessionCostTracker(settings)
        self.faster_stt = runtime.faster_stt
        self.recording_stt = runtime.recording_stt
        self.telephony_context = telephony_context
        self.defer_initial_prompt = defer_initial_prompt
        self.session_id = str(session_id or "").strip() or datetime.utcnow().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
        self.artifacts = SessionArtifacts(
            client_id=client_id,
            project_id=self.project.project_id if self.project else None,
            project_name=self.project.name if self.project else None,
            session_id=self.session_id,
            started_at=datetime.utcnow(),
        )
        if isinstance(self.telephony_context, dict):
            self.artifacts.telephony_context = dict(self.telephony_context)
        initial_lead_name = self._sanitize_captured_lead_name(self.customer_name)
        if initial_lead_name:
            self.artifacts.memory.lead_name = initial_lead_name
        self.artifacts.memory.contact_details = dict(self.contact_details)
        self.session_dir = self.session_logger.create_session_dir(client_id, self.session_id)
        self._running = True
        self._turn_buffers: dict[str, list[str]] = defaultdict(list)
        self._partial_turns: dict[str, str] = {}
        self._turn_index = 0
        self._status_key: tuple[str, str] | None = None
        self._agent_audio_deadline = 0.0
        self._last_user_turn_at = monotonic()
        self._last_user_transcription_at = 0.0
        self._last_agent_turn_at = 0.0
        self._waiting_for_user_after_agent = False
        self._silence_follow_up_sent = False
        self._opening_delivered = False
        self._has_user_spoken = False
        self._latest_user_snippet = ""
        self._latest_user_snippet_final = False
        self._latest_user_snippet_at = 0.0
        self._last_playback_flush_at = 0.0
        self._pending_native_line: str | None = None
        self._native_line_in_flight = False
        self._native_line_completed = asyncio.Event()
        self._native_line_completed.set()
        self._scripted_flow_stage = "none"
        self._appointment_date_value: str | None = None
        self._appointment_time_value: str | None = None
        self._shutdown_lock = asyncio.Lock()
        self._shutdown_started = False
        self._stop_cleanup_task: asyncio.Task[None] | None = None
        self._auto_stop_task: asyncio.Task[None] | None = None
        self._early_commit_task: asyncio.Task[None] | None = None
        self._partial_commit_watchdog_task: asyncio.Task[None] | None = None
        self._low_confidence_reprompt_task: asyncio.Task[None] | None = None
        self._deterministic_followup_task: asyncio.Task[None] | None = None
        self._silence_followup_watchdog_task: asyncio.Task[None] | None = None
        self._last_committed_user_text = ""
        self._last_user_intents: tuple[str, ...] = ()
        self._last_user_audio_activity_at = 0.0
        self._last_low_confidence_reprompt_at = 0.0
        self._awaiting_binary_confirmation = False
        self._binary_confirmation_prompted_at = 0.0
        self._binary_confirmation_resolved = False
        self._input_audio_capture = bytearray()
        self._agent_audio_capture = bytearray()
        self._conversation_audio_capture = bytearray()
        self._input_audio_peak = 0
        self._input_audio_avg_total = 0.0
        self._input_audio_avg_samples = 0
        self._agent_audio_peak = 0
        self._agent_audio_avg_total = 0.0
        self._agent_audio_avg_samples = 0
        self._parallel_stt_buffer = bytearray()
        self._parallel_stt_tasks: set[asyncio.Task[str]] = set()
        self._parallel_stt_segments: list[str] = []
        self._parallel_stt_sample_rate = 16_000
        configured_segment_seconds = max(float(self.settings.parallel_stt_segment_seconds or 0.0), PARALLEL_STT_MIN_SEGMENT_SECONDS)
        self._parallel_stt_segment_bytes = int(self._parallel_stt_sample_rate * 2 * configured_segment_seconds)
        self._initial_prompt_ready = asyncio.Event()
        if not defer_initial_prompt:
            self._initial_prompt_ready.set()

    async def run(self) -> Path:
        """Run the local demo until interrupted."""
        self._print_banner()
        await self._emit(
            "lifecycle",
            stage="starting",
            client_id=self.client.config.client_id,
            customer_name=self.customer_name,
            opening_language=self.opening_language,
        )
        receiver_task: asyncio.Task[None] | None = None
        sender_task: asyncio.Task[None] | None = None
        partial_commit_watchdog_task: asyncio.Task[None] | None = None
        low_confidence_reprompt_task: asyncio.Task[None] | None = None
        silence_followup_watchdog_task: asyncio.Task[None] | None = None
        live_reconnect_attempts = 0
        try:
            self.audio.open()
            system_prompt = self.prompt_builder.build(
                self.client,
                self.artifacts.memory,
                customer_name=self.customer_name,
                opening_language=self.opening_language,
                contact_details=self.contact_details,
            )
            while self._running:
                await self.live.connect(
                    system_prompt=system_prompt,
                    voice_name=self.client.config.voice.voice_name,
                    generation_settings=self.client.config.live_generation,
                    response_mode=self.settings.agent_response_mode,
                    explicit_vad=self._should_use_explicit_vad(),
                )
                receiver_task = asyncio.create_task(self._receive_loop())
                sender_task = asyncio.create_task(self._send_loop())
                partial_commit_watchdog_task = asyncio.create_task(self._partial_commit_watchdog())
                low_confidence_reprompt_task = asyncio.create_task(self._low_confidence_reprompt_watchdog())
                silence_followup_watchdog_task = asyncio.create_task(self._silence_followup_watchdog())
                try:
                    if not self._opening_delivered:
                        await self._initial_prompt_ready.wait()
                        await self._deliver_native_agent_line(self._render_opening_text())
                    elif live_reconnect_attempts > 0:
                        await self._deliver_native_agent_line(self._render_live_reconnect_line())
                    await asyncio.gather(
                        receiver_task,
                        sender_task,
                        partial_commit_watchdog_task,
                        low_confidence_reprompt_task,
                        silence_followup_watchdog_task,
                    )
                    break
                except Exception as exc:
                    retryable = self._is_retryable_live_error(exc)
                    if retryable and live_reconnect_attempts < MAX_LIVE_RECONNECT_ATTEMPTS and self._running:
                        live_reconnect_attempts += 1
                        logger.warning(
                            "Gemini Live transient failure; reconnecting session_id=%s attempt=%s error=%s",
                            self.session_id,
                            live_reconnect_attempts,
                            self._describe_exception(exc),
                        )
                        self._set_status("reconnecting", "Live connection dropped. Reconnecting...")
                        with contextlib.suppress(Exception):
                            await self._emit(
                                "warning",
                                message="Live connection dropped. Reconnecting the same call.",
                                session_id=self.session_id,
                                attempt=live_reconnect_attempts,
                            )
                        for task in (
                            receiver_task,
                            sender_task,
                            partial_commit_watchdog_task,
                            low_confidence_reprompt_task,
                            silence_followup_watchdog_task,
                        ):
                            if task is not None:
                                task.cancel()
                                await self._await_shutdown_task(task)
                        receiver_task = None
                        sender_task = None
                        partial_commit_watchdog_task = None
                        low_confidence_reprompt_task = None
                        silence_followup_watchdog_task = None
                        with contextlib.suppress(Exception):
                            await self.live.close()
                        await asyncio.sleep(0.15)
                        continue
                    raise
        except KeyboardInterrupt:
            self.console.print("\nStopping session...")
        except Exception as exc:
            if self._is_clean_disconnect(exc):
                message = "Gemini Live session ended normally. You can start a new session from the dashboard."
                self.console.print(message)
                self._set_status("finished", message)
            else:
                message = self._describe_exception(exc)
                logger.exception("Session failed")
                self.artifacts.errors.append(message)
                self.console.print(f"[red]Error:[/red] {message}")
                await self._emit("error", message=message)
        finally:
            self._running = False
            for task in (
                receiver_task,
                sender_task,
                partial_commit_watchdog_task,
                low_confidence_reprompt_task,
                silence_followup_watchdog_task,
                self._deterministic_followup_task,
            ):
                if task is not None:
                    task.cancel()
                    await self._await_shutdown_task(task)
            await self._finalize()
        return self.session_dir

    def stop(self) -> None:
        """Request a graceful session shutdown from the CLI or web UI."""
        self._running = False
        if self._status_key != ("stopping", "Stopping session..."):
            self._set_status("stopping", "Stopping session...")

        if self._stop_cleanup_task is None or self._stop_cleanup_task.done():
            with contextlib.suppress(RuntimeError):
                loop = asyncio.get_running_loop()
                self._stop_cleanup_task = loop.create_task(self._begin_shutdown())

    def release_initial_prompt(self) -> None:
        """Allow the opening line to be delivered once telephony media is ready."""
        self._initial_prompt_ready.set()

    async def _send_loop(self) -> None:
        self._set_status("listening", "Speak naturally. Press Ctrl+C to end the demo.")
        browser_activity_open = False
        browser_silence_started_at: float | None = None
        async for chunk in self.audio.mic_chunks():
            if not self._running:
                return
            if self.audio.is_playing() and self.audio.should_drop_input_while_playing():
                continue
            remaining_cooldown = self._agent_audio_deadline - monotonic()
            if remaining_cooldown > 0:
                await asyncio.sleep(remaining_cooldown)
                if not self._running:
                    return
            self._capture_input_audio_stats(chunk)
            self._capture_parallel_stt_chunk(chunk)
            self.cost_tracker.record_live_input_audio(chunk)
            await self.live.send_audio(chunk)
            if self._is_browser_session() and self._should_use_explicit_vad():
                avg = self._chunk_average_amplitude(chunk)
                if avg >= BROWSER_EXPLICIT_VAD_AUDIO_THRESHOLD:
                    browser_activity_open = True
                    browser_silence_started_at = None
                elif browser_activity_open:
                    if browser_silence_started_at is None:
                        browser_silence_started_at = monotonic()
                    elif (monotonic() - browser_silence_started_at) >= BROWSER_EXPLICIT_VAD_END_SECONDS:
                        await self._signal_live_activity_end()
                        browser_activity_open = False
                        browser_silence_started_at = None
        if browser_activity_open and self._is_browser_session() and self._should_use_explicit_vad():
            await self._signal_live_activity_end()
        self._running = False

    async def _receive_loop(self) -> None:
        async for event in self.live.receive():
            if not self._running:
                return
            await self._handle_live_event(event)
        self._running = False

    async def _handle_live_event(self, event: LiveEvent) -> None:
        if event.kind == "audio" and event.audio:
            if self._deterministic_followup_task is not None and not self._deterministic_followup_task.done():
                self._deterministic_followup_task.cancel()
            self.cost_tracker.record_live_output_audio(event.audio)
            self._capture_agent_audio_stats(event.audio, sample_rate=24_000)
            self._agent_audio_deadline = monotonic() + AGENT_PLAYBACK_COOLDOWN_SECONDS
            self._set_status("speaking", "Gemini is responding...")
            await self.audio.play(event.audio)
            if event.latency_ms is not None:
                self.artifacts.metrics["latest_first_audio_latency_ms"] = event.latency_ms
                await self._emit("metric", latest_first_audio_latency_ms=event.latency_ms)
            return

        if event.kind == "interrupted":
            if not self._should_flush_for_barge_in():
                logger.debug("Ignoring interruption event without recent user speech.")
                return
            self._agent_audio_deadline = 0.0
            self._last_playback_flush_at = monotonic()
            self._set_status("listening", "Prospect interruption detected. Clearing playback buffer.")
            await self.audio.flush_playback()
            return

        if event.kind == "user_text" and event.text:
            self._binary_confirmation_resolved = True
            self._awaiting_binary_confirmation = False
            self._capture_text_event("user", event.text, event.is_final)
            self._last_user_turn_at = monotonic()
            self._last_user_transcription_at = monotonic()
            self._has_user_spoken = True
            self._latest_user_snippet = " ".join(event.text.split()).strip()
            self._latest_user_snippet_final = event.is_final
            self._latest_user_snippet_at = monotonic()
            self.current_language = detect_conversation_language(event.text, fallback=self.current_language)
            self._waiting_for_user_after_agent = False
            self._silence_follow_up_sent = False
            if self._opening_delivered:
                self.artifacts.memory.next_step = "Move directly into discovery without reconfirming the name."
            self._schedule_short_response_commit(event.text, event.is_final)
            self._set_status("processing", "Prospect turn detected. Waiting for response...")
            return

        if event.kind == "agent_text" and event.text:
            if self._deterministic_followup_task is not None and not self._deterministic_followup_task.done():
                self._deterministic_followup_task.cancel()
            self._mark_binary_confirmation_state(event.text)
            normalized_candidate = " ".join(event.text.split()).strip()
            if self._pending_native_line:
                pending = " ".join(self._pending_native_line.split()).strip()
                if normalized_candidate and (normalized_candidate in pending or pending in normalized_candidate):
                    return
            if event.source == "output_transcription":
                if len(normalized_candidate.split()) <= 2 and len(normalized_candidate) <= 14:
                    return
            self._capture_text_event("agent", event.text, event.is_final)
            return

        if event.kind == "turn_complete":
            self._cancel_early_commit_task()
            self._agent_audio_deadline = monotonic() + AGENT_PLAYBACK_COOLDOWN_SECONDS
            if self._pending_native_line is not None:
                self._native_line_in_flight = False
                self._native_line_completed.set()
                self._last_agent_turn_at = monotonic()
                self._waiting_for_user_after_agent = True
            self._pending_native_line = None
            await self._commit_turns(event.latency_ms)
            if self._running:
                self._set_status("listening", "Ready for the next turn.")

    def _capture_text_event(self, speaker: str, text: str, is_final: bool) -> None:
        normalized = " ".join(text.split()).strip()
        if not normalized:
            return

        previous = self._partial_turns.get(speaker, "")
        if previous == normalized:
            return

        if previous and normalized.startswith(previous):
            self._partial_turns[speaker] = normalized
        elif previous and previous.startswith(normalized):
            return
        else:
            self._partial_turns[speaker] = normalized

        if is_final:
            self._turn_buffers[speaker].append(self._partial_turns.pop(speaker))

    async def _commit_turns(self, latency_ms: float | None) -> None:
        self._cancel_early_commit_task()
        for speaker, pending_text in list(self._partial_turns.items()):
            if pending_text:
                self._turn_buffers[speaker].append(pending_text)
            self._partial_turns.pop(speaker, None)

        user_text = self._normalize_user_commit_text(self._turn_buffers.pop("user", []))
        agent_text = " ".join(self._turn_buffers.pop("agent", [])).strip()
        if user_text:
            self._turn_index += 1
            self._last_user_intents = tuple(self._classify_user_intents(user_text))
            logger.info(
                "Classified user intents: stage=%s text=%r intents=%s",
                self._scripted_flow_stage,
                user_text,
                list(self._last_user_intents),
            )
            self._awaiting_binary_confirmation = False
            self._binary_confirmation_resolved = True
            self._update_lead_identity_from_user_text(user_text, self._last_user_intents)
            self.artifacts.memory = update_memory_from_user_text(self.artifacts.memory, user_text)
            self.session_logger.append_turn(
                self.artifacts,
                TranscriptTurn(speaker="user", text=user_text, turn_id=self._turn_index),
            )
            self._last_committed_user_text = user_text
            self.console.print(f"[bold cyan]Prospect:[/bold cyan] {user_text}")
            asyncio.create_task(self._emit("turn", speaker="user", text=user_text, turn_id=self._turn_index))
            asyncio.create_task(self._emit("user_intents", intents=list(self._last_user_intents), text=user_text))
            if self._is_closing_turn("user", user_text):
                self.artifacts.memory.next_step = self.artifacts.memory.next_step or user_text
                self._waiting_for_user_after_agent = False
                self._silence_follow_up_sent = True
                self.stop()
                return
            self._schedule_deterministic_followup(user_text, self._last_user_intents)
        if agent_text:
            agent_text = self._normalize_agent_text(agent_text)
            if not agent_text:
                return
            self._mark_binary_confirmation_state(agent_text)
            self.session_logger.append_turn(
                self.artifacts,
                TranscriptTurn(
                    speaker="agent",
                    text=agent_text,
                    turn_id=self._turn_index,
                    latency_ms=latency_ms,
                ),
            )
            self._last_agent_turn_at = monotonic()
            self._waiting_for_user_after_agent = True
            if self.live.response_mode == "text_tts":
                self._agent_audio_deadline = monotonic() + AGENT_PLAYBACK_COOLDOWN_SECONDS
                self._set_status("speaking", "Playing Gemini response...")
                audio = await self.speech.synthesize(agent_text, self.client.config.voice.voice_name)
                self.cost_tracker.record_tts(agent_text, audio)
                self._capture_agent_audio_stats(audio, sample_rate=24_000)
                await self.audio.play(audio)
            self.cost_tracker.record_agent_text(agent_text)
            self.console.print(f"[bold green]Agent:[/bold green] {agent_text}")
            asyncio.create_task(
                self._emit(
                    "turn",
                    speaker="agent",
                    text=agent_text,
                    turn_id=self._turn_index,
                    latency_ms=latency_ms,
                )
            )
            if self._scripted_flow_stage == "closing_confirmation":
                self._waiting_for_user_after_agent = False
                self._silence_follow_up_sent = True
                self._awaiting_binary_confirmation = False
                self._binary_confirmation_resolved = True
                self._schedule_auto_stop()
                return
            if self._is_closing_turn("agent", agent_text):
                self.artifacts.memory.next_step = self.artifacts.memory.next_step or agent_text
                self._waiting_for_user_after_agent = False
                self._silence_follow_up_sent = True
                self._schedule_auto_stop()
            elif self._should_auto_stop_after_agent_turn(agent_text):
                self.artifacts.memory.next_step = self.artifacts.memory.next_step or agent_text
                self._waiting_for_user_after_agent = False
                self._silence_follow_up_sent = True
                self._schedule_auto_stop()
        if latency_ms is not None:
            self.artifacts.metrics["latest_turn_latency_ms"] = latency_ms
            asyncio.create_task(self._emit("metric", latest_turn_latency_ms=latency_ms))

    async def _finalize(self) -> None:
        if self._partial_turns or any(self._turn_buffers.values()):
            logger.debug("Flushing pending turns before final summary generation.")
            await self._commit_turns(None)
        self.artifacts.ended_at = datetime.utcnow()
        if isinstance(self.telephony_context, dict):
            self.artifacts.telephony_context = dict(self.telephony_context)
        await self._begin_shutdown()
        self._save_input_audio_capture()
        # Persist a fast fallback snapshot immediately on disconnect so UI has
        # phone/result/follow-up data even if enrichment takes longer.
        if self.artifacts.summary is None:
            fallback_summary = self._build_transcript_fallback_summary()
            self.artifacts.summary = fallback_summary
            self.artifacts.memory = merge_memory(self.artifacts.memory, fallback_summary)
        self._persist_call_outcome_snapshot("early_finalize")

        try:
            await self._finalize_parallel_stt()
        except Exception as exc:
            self.artifacts.errors.append(f"Parallel STT finalize failed at finalize stage: {exc}")

        try:
            await self._extract_recording_corpus_and_details()
        except Exception as exc:
            self.artifacts.errors.append(f"Recording corpus enrichment stage failed: {exc}")
        self._persist_call_outcome_snapshot("pre_enrichment")

        try:
            await self._augment_transcript_with_faster_stt_fallback()
        except Exception as exc:
            logger.warning("Faster-Whisper fallback failed: %s", exc)
            self.artifacts.errors.append(f"Faster-Whisper fallback failed: {exc}")

        if self.artifacts.transcript:
            summary: PostCallSummary | None = None
            summary_error: Exception | None = None
            structured_prompt = build_extraction_prompt(self.client, self.artifacts)
            self.cost_tracker.record_structured_request(structured_prompt)
            for attempt in range(1, POST_CALL_SUMMARY_MAX_ATTEMPTS + 1):
                use_fallback = self.structured_fallback is not None and attempt >= 2
                structured_client = self.structured_fallback if use_fallback else self.structured
                try:
                    # TODO: This post-call step can later move behind an async queue or telephony
                    # workflow trigger without changing the live conversation engine.
                    summary = await asyncio.wait_for(
                        structured_client.summarize_call(self.client, self.artifacts),
                        timeout=POST_CALL_SUMMARY_TIMEOUT_SECONDS,
                    )
                    if attempt > 1:
                        self.artifacts.metrics["summary_retry_success_attempt"] = float(attempt)
                    break
                except Exception as exc:
                    summary_error = exc
                    retryable = isinstance(exc, TimeoutError) or self._is_retryable_structured_error(exc)
                    if not retryable or attempt >= POST_CALL_SUMMARY_MAX_ATTEMPTS:
                        break
                    await asyncio.sleep(POST_CALL_SUMMARY_RETRY_BACKOFF_SECONDS * attempt)

            if summary is not None:
                self.cost_tracker.record_structured_response(summary.summary)
                self.cost_tracker.record_structured_response(summary.suggested_next_action)
                self.artifacts.summary = summary
                self.artifacts.memory = merge_memory(self.artifacts.memory, summary)
                await self._emit(
                    "summary",
                    summary=summary.summary,
                    suggested_next_action=summary.suggested_next_action,
                    qualification=summary.qualification.model_dump(mode="json"),
                )
            else:
                if isinstance(summary_error, TimeoutError):
                    timeout_message = (
                        f"Summary generation timed out after {POST_CALL_SUMMARY_TIMEOUT_SECONDS:.1f}s "
                        f"(attempted {POST_CALL_SUMMARY_MAX_ATTEMPTS} times)"
                    )
                    logger.warning(timeout_message)
                    self.artifacts.errors.append(timeout_message)
                elif summary_error is not None:
                    logger.warning("Summary generation failed: %s", summary_error)
                    self.artifacts.errors.append(f"Summary generation failed: {summary_error}")
                fallback_summary = self._build_transcript_fallback_summary()
                self.artifacts.summary = fallback_summary
                self.artifacts.memory = merge_memory(self.artifacts.memory, fallback_summary)
                await self._emit(
                    "summary",
                    summary=fallback_summary.summary,
                    suggested_next_action=fallback_summary.suggested_next_action,
                    qualification=fallback_summary.qualification.model_dump(mode="json"),
                )

        self.artifacts.actual_cost = self.cost_tracker.build_ledger(self.artifacts, self.telephony_context)
        integration_errors = trigger_post_call_integrations(self.client, self.artifacts, self.telephony_context)
        if integration_errors:
            self.artifacts.errors.extend(integration_errors)
        self._persist_call_outcome_snapshot("post_enrichment")
        await self._emit(
            "lifecycle",
            stage="finished",
            session_dir=str(self.session_dir),
            session_id=self.session_id,
        )
        self._print_summary_panel()

    def _persist_call_outcome_snapshot(self, stage: str) -> None:
        try:
            self.call_outcome_store.upsert_outcome(self.artifacts, self.telephony_context)
        except Exception as exc:
            logger.exception("Failed to persist call outcome for session_id=%s stage=%s", self.session_id, stage)
            self.artifacts.errors.append(f"Call outcome DB persistence failed: {exc}")
        self._record_telephony_timing_metrics()
        try:
            self.session_logger.save(self.artifacts, self.session_dir)
        except Exception as exc:
            logger.exception("Failed to save artifacts for session_id=%s stage=%s", self.session_id, stage)
            self.artifacts.errors.append(f"Artifacts save failed: {exc}")

    def _build_transcript_corpus_fallback(self) -> str:
        parts: list[str] = []
        for turn in self.artifacts.transcript:
            text = " ".join(str(turn.text or "").split()).strip()
            if not text:
                continue
            speaker = " ".join(str(turn.speaker or "").split()).strip() or "user"
            parts.append(f"{speaker}: {text}")
        return " ".join(parts).strip()

    def _extract_important_questions_fallback(self) -> list[str]:
        question_words = (
            "what",
            "when",
            "where",
            "which",
            "who",
            "why",
            "how",
            "can i",
            "could i",
            "should i",
            "is it",
            "do i",
            "क्या",
            "कब",
            "कौन",
            "किस",
            "कैसे",
            "क्यों",
            "काय",
            "कधी",
            "कोण",
            "कुठे",
            "का",
            "कसा",
            "कशी",
        )
        candidates: list[str] = []
        for turn in self.artifacts.transcript:
            if str(turn.speaker or "").strip().lower() != "user":
                continue
            text = " ".join(str(turn.text or "").split()).strip()
            if not text:
                continue
            if len(text) < 6:
                continue
            lower = text.lower()
            has_qmark = "?" in text or "？" in text
            has_qword = any(token in lower for token in question_words)
            # Keep stricter gating to avoid noisy single-token captures.
            if has_qmark or (has_qword and len(text.split()) >= 3):
                candidates.append(text[:180])
        deduped: list[str] = []
        seen: set[str] = set()
        for item in candidates:
            key = item.strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(item)
            if len(deduped) >= 10:
                break
        return deduped

    def _build_transcript_fallback_summary(self) -> PostCallSummary:
        user_turns = [
            " ".join(str(turn.text or "").split()).strip()
            for turn in self.artifacts.transcript
            if turn.speaker == "user" and str(turn.text or "").strip()
        ]
        agent_turns = [
            " ".join(str(turn.text or "").split()).strip()
            for turn in self.artifacts.transcript
            if turn.speaker == "agent" and str(turn.text or "").strip()
        ]
        contact_details = {
            str(k): str(v).strip()
            for k, v in {
                **(self.contact_details or {}),
                **(self.artifacts.memory.contact_details or {}),
            }.items()
            if str(v).strip()
        }
        lead_name = self._sanitize_captured_lead_name(self.artifacts.memory.lead_name) or self._sanitize_captured_lead_name(
            self.customer_name
        )
        if user_turns:
            recent_user = " | ".join(user_turns[-3:])
            summary = f"Call completed with {lead_name or 'the prospect'}. Key user responses: {recent_user}"
            if len(summary) > 420:
                summary = f"Call completed with {lead_name or 'the prospect'}. Key user responses: {recent_user[:380]}..."
            interest = "medium"
            next_step = "Follow up with the prospect using the captured requirements and confirm next steps."
        else:
            opener = agent_turns[0] if agent_turns else "the agent introduced the service and asked qualification questions"
            summary = (
                f"Call completed with {lead_name or 'the prospect'}, but no clear user response was captured. "
                f"Agent context: {opener}."
            )
            interest = "unknown"
            next_step = "Retry outreach and confirm the prospect's requirement, timeline, and preferred callback time."
        return PostCallSummary(
            lead_name=lead_name,
            contact_details=contact_details,
            interest_level=interest,
            summary=summary,
            suggested_next_action=next_step,
        )

    def _capture_input_audio_stats(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._input_audio_capture.extend(chunk)
        self._conversation_audio_capture.extend(chunk)
        avg, peak, sample_count = self._chunk_amplitude_stats(chunk)
        if peak <= 0 or sample_count <= 0:
            return
        self._input_audio_peak = max(self._input_audio_peak, peak)
        self._input_audio_avg_total += avg * sample_count
        self._input_audio_avg_samples += sample_count
        if avg >= self._binary_confirmation_audio_threshold():
            self._last_user_audio_activity_at = monotonic()
        if self._is_partial_commit_session() and avg >= self.settings.exotel_low_confidence_audio_threshold:
            self._last_user_audio_activity_at = monotonic()

    @staticmethod
    def _chunk_amplitude_stats(chunk: bytes) -> tuple[float, int, int]:
        samples = array("h")
        samples.frombytes(chunk)
        if not samples:
            return 0.0, 0, 0
        peak = max(abs(sample) for sample in samples)
        avg = sum(abs(sample) for sample in samples) / len(samples)
        return avg, peak, len(samples)

    @classmethod
    def _chunk_average_amplitude(cls, chunk: bytes) -> float:
        avg, _peak, _count = cls._chunk_amplitude_stats(chunk)
        return avg

    def _resample_pcm16_mono(self, pcm: bytes, source_sample_rate: int, target_sample_rate: int) -> bytes:
        if not pcm:
            return b""
        if source_sample_rate == target_sample_rate:
            return pcm
        try:
            src_samples = array("h")
            src_samples.frombytes(pcm)
            if not src_samples:
                return b""
            if source_sample_rate <= 0 or target_sample_rate <= 0:
                return b""
            target_length = max(1, int(len(src_samples) * target_sample_rate / source_sample_rate))
            if target_length == len(src_samples):
                return src_samples.tobytes()

            dst_samples = array("h", [0] * target_length)
            if len(src_samples) == 1:
                dst_samples = array("h", [src_samples[0]] * target_length)
                return dst_samples.tobytes()

            step = (len(src_samples) - 1) / max(target_length - 1, 1)
            for i in range(target_length):
                pos = i * step
                left = int(pos)
                right = min(left + 1, len(src_samples) - 1)
                frac = pos - left
                value = int((1.0 - frac) * src_samples[left] + frac * src_samples[right])
                if value > 32767:
                    value = 32767
                elif value < -32768:
                    value = -32768
                dst_samples[i] = value
            return dst_samples.tobytes()
        except Exception as exc:
            logger.warning(
                "Failed to resample audio: session_id=%s source_sample_rate=%s target_sample_rate=%s error=%s",
                self.session_id,
                source_sample_rate,
                target_sample_rate,
                exc,
            )
            return b""

    def _capture_agent_audio_stats(self, chunk: bytes, sample_rate: int) -> None:
        if not chunk:
            return
        chunk_24k = self._resample_pcm16_mono(chunk, source_sample_rate=sample_rate, target_sample_rate=24_000)
        if chunk_24k:
            self._agent_audio_capture.extend(chunk_24k)
            samples = array("h")
            samples.frombytes(chunk_24k)
            if samples:
                peak = max(abs(sample) for sample in samples)
                avg = sum(abs(sample) for sample in samples) / len(samples)
                self._agent_audio_peak = max(self._agent_audio_peak, peak)
                self._agent_audio_avg_total += avg * len(samples)
                self._agent_audio_avg_samples += len(samples)

        chunk_16k = self._resample_pcm16_mono(chunk, source_sample_rate=sample_rate, target_sample_rate=16_000)
        if chunk_16k:
            self._conversation_audio_capture.extend(chunk_16k)

    def _parallel_stt_active(self) -> bool:
        if not self.settings.parallel_stt_enabled:
            return False
        if not self.faster_stt.enabled:
            return False
        return self._is_exotel_session() or self._is_twilio_session() or self._is_meta_whatsapp_session()

    def _capture_parallel_stt_chunk(self, chunk: bytes) -> None:
        if not chunk or not self._parallel_stt_active():
            return
        self._parallel_stt_buffer.extend(chunk)
        if len(self._parallel_stt_buffer) < self._parallel_stt_segment_bytes:
            return
        self._enqueue_parallel_stt_segment(bytes(self._parallel_stt_buffer))
        self._parallel_stt_buffer.clear()

    def _enqueue_parallel_stt_segment(self, pcm_segment: bytes) -> None:
        if not pcm_segment or not self._parallel_stt_active():
            return
        with contextlib.suppress(RuntimeError):
            loop = asyncio.get_running_loop()
            task = loop.create_task(self._transcribe_parallel_stt_segment(pcm_segment))
            self._parallel_stt_tasks.add(task)
            task.add_done_callback(lambda completed: self._parallel_stt_tasks.discard(completed))

    async def _transcribe_parallel_stt_segment(self, pcm_segment: bytes) -> str:
        text = " ".join(
            (
                await self.faster_stt.transcribe_pcm16_mono_async(
                    pcm_segment,
                    sample_rate=self._parallel_stt_sample_rate,
                    language_hint=self.current_language,
                )
            ).split()
        ).strip()
        if text:
            self._parallel_stt_segments.append(text)
        return text

    async def _finalize_parallel_stt(self) -> None:
        if not self._parallel_stt_active():
            return
        if self._parallel_stt_buffer:
            self._enqueue_parallel_stt_segment(bytes(self._parallel_stt_buffer))
            self._parallel_stt_buffer.clear()
        if self._parallel_stt_tasks:
            try:
                await asyncio.gather(*self._parallel_stt_tasks, return_exceptions=True)
            except Exception as exc:
                logger.warning("Parallel STT finalize failed for session_id=%s error=%s", self.session_id, exc)
                self.artifacts.errors.append(f"Parallel STT finalize failed: {exc}")

        merged = " ".join(" ".join(str(item).split()).strip() for item in self._parallel_stt_segments if str(item).strip()).strip()
        if not merged:
            return
        existing_user_text = " ".join(
            " ".join(str(turn.text or "").split()).strip()
            for turn in self.artifacts.transcript
            if turn.speaker == "user" and str(turn.text or "").strip()
        ).strip().lower()
        normalized_merged = merged.lower()
        if existing_user_text and (normalized_merged in existing_user_text):
            return

        self.artifacts.parallel_stt_segments = list(self._parallel_stt_segments)
        self.artifacts.parallel_stt_transcript = merged
        self.artifacts.metrics["parallel_stt_segments"] = float(len(self._parallel_stt_segments))
        self.artifacts.metrics["parallel_stt_chars"] = float(len(merged))
        self.artifacts.metrics["stt_parallel_mode_active"] = 1.0

        self._turn_index += 1
        turn = TranscriptTurn(speaker="user", text=merged, turn_id=self._turn_index)
        self.session_logger.append_turn(self.artifacts, turn)
        intents = tuple(self._classify_user_intents(merged))
        self._update_lead_identity_from_user_text(merged, intents)
        self.artifacts.memory = update_memory_from_user_text(self.artifacts.memory, merged)
        logger.info(
            "Augmented transcript via parallel STT: session_id=%s chars=%s segments=%s",
            self.session_id,
            len(merged),
            len(self._parallel_stt_segments),
        )

    async def _extract_recording_corpus_and_details(self) -> None:
        if not self.settings.recording_stt_enabled:
            return
        if not (self._is_exotel_session() or self._is_twilio_session() or self._is_meta_whatsapp_session()):
            return
        caller_wav_path = self.session_dir / "caller_audio.wav"
        conversation_wav_path = self.session_dir / "conversation_audio.wav"
        if not caller_wav_path.exists() and not conversation_wav_path.exists():
            return

        if conversation_wav_path.exists():
            try:
                full_corpus_text, full_meta = await self.recording_stt.transcribe_async(conversation_wav_path)
            except Exception as exc:
                self.artifacts.errors.append(f"Recording STT full-corpus transcription failed: {exc}")
                full_corpus_text, full_meta = "", {}
            full_corpus_text = " ".join(str(full_corpus_text or "").split()).strip()
            if full_corpus_text:
                self.artifacts.recording_stt_full_corpus = full_corpus_text
                self.artifacts.metrics["recording_stt_full_chars"] = float(len(full_corpus_text))
                full_mode = str(full_meta.get("mode") or "").strip().lower()
                self.artifacts.metrics["recording_stt_full_mode_chunked"] = 1.0 if full_mode == "chunked_recording" else 0.0
                # Use full conversation corpus as the primary extraction corpus.
                self.artifacts.recording_stt_corpus = full_corpus_text
                self.artifacts.metrics["recording_stt_chars"] = float(len(full_corpus_text))
                self.artifacts.metrics["recording_stt_mode_chunked"] = 1.0 if full_mode == "chunked_recording" else 0.0

        corpus_text = " ".join(str(self.artifacts.recording_stt_corpus or "").split()).strip()
        if not corpus_text:
            source_wav_path = caller_wav_path if caller_wav_path.exists() else conversation_wav_path
            try:
                corpus_text, meta = await self.recording_stt.transcribe_async(source_wav_path)
            except Exception as exc:
                self.artifacts.errors.append(f"Recording STT transcription failed: {exc}")
                corpus_text = ""
            corpus_text = " ".join(str(corpus_text or "").split()).strip()
            if corpus_text:
                self.artifacts.recording_stt_corpus = corpus_text
                self.artifacts.metrics["recording_stt_chars"] = float(len(corpus_text))
                mode = str(meta.get("mode") or "").strip().lower()
                self.artifacts.metrics["recording_stt_mode_chunked"] = 1.0 if mode == "chunked_recording" else 0.0

        # Extra recovery pass: if primary STT still empty, retry on conversation
        # WAV once more (with internal SR retries) before transcript fallback.
        if not corpus_text and conversation_wav_path.exists():
            try:
                retry_text, retry_meta = await self.recording_stt.transcribe_async(conversation_wav_path)
            except Exception as exc:
                self.artifacts.errors.append(f"Recording STT retry failed: {exc}")
                retry_text = ""
                retry_meta = {}
            retry_text = " ".join(str(retry_text or "").split()).strip()
            if retry_text:
                corpus_text = retry_text
                self.artifacts.recording_stt_corpus = retry_text
                self.artifacts.metrics["recording_stt_chars"] = float(len(retry_text))
                self.artifacts.metrics["recording_stt_retry_success"] = 1.0
                retry_mode = str(retry_meta.get("mode") or "").strip().lower()
                self.artifacts.metrics["recording_stt_mode_chunked"] = 1.0 if retry_mode == "chunked_recording" else 0.0

        if not corpus_text:
            corpus_text = self._build_transcript_corpus_fallback()
            if corpus_text:
                self.artifacts.recording_stt_corpus = corpus_text
                self.artifacts.metrics["recording_stt_chars"] = float(len(corpus_text))
                self.artifacts.metrics["recording_stt_from_transcript_fallback"] = 1.0
        if not corpus_text:
            return

        if not self.settings.recording_llm_extraction_enabled:
            return

        llm_corpus_text = " ".join(str(self.artifacts.recording_stt_full_corpus or corpus_text).split()).strip()
        booking_intent_detected = self._booking_intent_detected_from_corpus(llm_corpus_text)
        prompt = build_recording_details_prompt(
            llm_corpus_text,
            call_started_at=self.artifacts.started_at.isoformat(),
            booking_intent_detected=booking_intent_detected,
        )
        payload: dict[str, Any] | None = None
        last_error: Exception | None = None
        for attempt in range(1, RECORDING_DETAILS_EXTRACTION_MAX_ATTEMPTS + 1):
            use_fallback = self.structured_fallback is not None and attempt >= 2
            structured_client = self.structured_fallback if use_fallback else self.structured
            try:
                payload = await asyncio.wait_for(
                    structured_client.generate_json(prompt=prompt, temperature=0.1),
                    timeout=RECORDING_DETAILS_EXTRACTION_TIMEOUT_SECONDS,
                )
                if isinstance(payload, dict) and payload:
                    if attempt > 1:
                        self.artifacts.metrics["recording_llm_retry_success_attempt"] = float(attempt)
                    break
                payload = None
            except TimeoutError as exc:
                last_error = exc
                logger.warning(
                    "Recording details extraction timed out session_id=%s attempt=%s/%s timeout=%.1fs",
                    self.session_id,
                    attempt,
                    RECORDING_DETAILS_EXTRACTION_MAX_ATTEMPTS,
                    RECORDING_DETAILS_EXTRACTION_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Recording details extraction failed session_id=%s attempt=%s/%s error=%s",
                    self.session_id,
                    attempt,
                    RECORDING_DETAILS_EXTRACTION_MAX_ATTEMPTS,
                    exc,
                )
                if not self._is_retryable_structured_error(exc):
                    break

            if attempt >= RECORDING_DETAILS_EXTRACTION_MAX_ATTEMPTS:
                break
            await asyncio.sleep(RECORDING_DETAILS_RETRY_BACKOFF_SECONDS * attempt)

        if not isinstance(payload, dict) or not payload:
            self.artifacts.metrics["recording_llm_extracted"] = 0.0
            self.artifacts.metrics["recording_llm_available"] = 0.0
            if isinstance(last_error, TimeoutError):
                self.artifacts.errors.append(
                    f"Recording details extraction timed out after {RECORDING_DETAILS_EXTRACTION_TIMEOUT_SECONDS:.1f}s"
                )
                self.artifacts.errors.append("Gemini structured extraction unavailable for this call.")
            elif last_error is not None:
                self.artifacts.errors.append(f"Recording details extraction failed: {last_error}")
                if self._is_retryable_structured_error(last_error):
                    self.artifacts.errors.append("Gemini structured extraction unavailable for this call.")
            else:
                self.artifacts.errors.append("Recording details extraction returned empty payload.")
                self.artifacts.errors.append("Gemini structured extraction unavailable for this call.")
            payload = {}

        self.artifacts.metrics["recording_llm_attempts_used"] = float(
            self.artifacts.metrics.get("recording_llm_retry_success_attempt") or 1.0
        )
        self.artifacts.metrics["recording_llm_available"] = 1.0

        raw_name = self._sanitize_captured_lead_name(str(payload.get("name") or "").strip())
        raw_age = payload.get("age")
        age_value: int | None = None
        if isinstance(raw_age, int) and 0 < raw_age < 120:
            age_value = raw_age
        elif isinstance(raw_age, str) and raw_age.strip().isdigit():
            parsed_age = int(raw_age.strip())
            if 0 < parsed_age < 120:
                age_value = parsed_age

        def _parse_corpus_date_fallback(corpus: str, started_at: datetime) -> str | None:
            text = " ".join(str(corpus or "").split()).strip().lower()
            if not text:
                return None
            relative_tokens = ("tomorrow", "कल", "उद्या", "उद्याची", "उद्याला")
            if any(token in text for token in relative_tokens):
                try:
                    return (started_at + timedelta(days=1)).strftime("%Y-%m-%d")
                except Exception:
                    return None
            month_map = {
                "january": 1, "jan": 1, "जनवरी": 1, "जानेवारी": 1,
                "february": 2, "feb": 2, "फरवरी": 2, "फेब्रुवारी": 2,
                "march": 3, "mar": 3, "मार्च": 3,
                "april": 4, "apr": 4, "अप्रैल": 4, "एप्रिल": 4,
                "may": 5, "मई": 5, "मे": 5,
                "june": 6, "jun": 6, "जून": 6,
                "july": 7, "jul": 7, "जुलाई": 7, "जुलै": 7,
                "august": 8, "aug": 8, "अगस्त": 8, "ऑगस्ट": 8,
                "september": 9, "sep": 9, "sept": 9, "सितंबर": 9, "सप्टेंबर": 9,
                "october": 10, "oct": 10, "अक्टूबर": 10, "ऑक्टोबर": 10,
                "november": 11, "nov": 11, "नवंबर": 11, "नोव्हेंबर": 11,
                "december": 12, "dec": 12, "दिसंबर": 12, "डिसेंबर": 12,
            }
            for month_token, month_num in month_map.items():
                pattern = re.compile(rf"(?<!\d)(\d{{1,2}})\s*{re.escape(month_token)}(?!\w)", re.IGNORECASE)
                match = pattern.search(text)
                if not match:
                    continue
                day = int(match.group(1))
                for year in (started_at.year, started_at.year + 1):
                    try:
                        candidate = datetime(year, month_num, day, tzinfo=started_at.tzinfo or None)
                    except ValueError:
                        continue
                    # Accept a date within one year window relative to call start.
                    if abs((candidate.date() - started_at.date()).days) <= 370:
                        return candidate.strftime("%Y-%m-%d")
            return None

        def _parse_corpus_name_fallback(corpus: str) -> str | None:
            text = " ".join(str(corpus or "").split()).strip()
            if not text:
                return None
            # Prefer explicit patient/caller name mentions first.
            patterns = (
                re.compile(r"(?:patient(?:'s)? name is|patient is|पेशंट(?:\s+है)?|रुग्ण(?:ाचे)? नाव)\s*[:\-]?\s*([^\d,.;!?]{2,60})", re.IGNORECASE),
                re.compile(r"(?:my name is|मेरा नाम|माझं नाव|माझे नाव)\s*[:\-]?\s*([^\d,.;!?]{2,60})", re.IGNORECASE),
            )
            for pattern in patterns:
                match = pattern.search(text)
                if not match:
                    continue
                candidate = self._sanitize_captured_lead_name(match.group(1))
                if candidate:
                    return candidate
            return None

        def _parse_corpus_time_fallback(corpus: str) -> str | None:
            text = " ".join(str(corpus or "").split()).strip().lower()
            if not text:
                return None
            # Numeric clock form first.
            numeric = re.search(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b", text)
            if numeric:
                return f"{int(numeric.group(1)):02d}:{numeric.group(2)}"
            meridiem = re.search(r"\b(\d{1,2})(?::([0-5]\d))?\s*(am|pm)\b", text, re.IGNORECASE)
            if meridiem:
                hour = int(meridiem.group(1))
                minute = int(meridiem.group(2) or "0")
                suffix = meridiem.group(3).lower()
                if suffix == "pm" and 1 <= hour <= 11:
                    hour += 12
                if suffix == "am" and hour == 12:
                    hour = 0
                if 0 <= hour <= 23:
                    return f"{hour:02d}:{minute:02d}"
            # Marathi/Hindi spoken hour map.
            hour_words = {
                "एक": 1, "दो": 2, "दोन": 2, "तीन": 3, "चार": 4, "पाच": 5, "सहा": 6,
                "सात": 7, "आठ": 8, "नऊ": 9, "दहा": 10, "अकरा": 11, "बारा": 12,
                "एक वाजता": 1, "दो वाजता": 2, "दोन वाजता": 2, "तीन वाजता": 3, "चार वाजता": 4,
                "पाच वाजता": 5, "सहा वाजता": 6, "सात वाजता": 7, "आठ वाजता": 8, "नऊ वाजता": 9,
                "दहा वाजता": 10, "अकरा वाजता": 11, "बारा वाजता": 12,
            }
            for token, hour in hour_words.items():
                if re.search(rf"(?<!\w){re.escape(token)}(?!\w)", text):
                    # Accept only explicit spoken time phrases.
                    if "वाजता" not in token and not re.search(rf"(?<!\w){re.escape(token)}\s*(वाजता|बजे)(?!\w)", text):
                        continue
                    return f"{hour:02d}:00"
            return None

        def _normalize_ymd(value: object) -> str | None:
            text = " ".join(str(value or "").split()).strip()
            if not text:
                return None
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
                try:
                    parsed = datetime.strptime(text, "%Y-%m-%d")
                    call_year = self.artifacts.started_at.year
                    # Guardrail: reject clearly wrong years from LLM hallucination.
                    if parsed.year < call_year or parsed.year > call_year + 1:
                        return None
                except Exception:
                    return None
                return text
            return None

        def _normalize_hhmm(value: object) -> str | None:
            text = " ".join(str(value or "").split()).strip()
            if not text:
                return None
            match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", text)
            if match:
                return f"{int(match.group(1)):02d}:{match.group(2)}"
            return None

        raw_questions = payload.get("important_questions_asked")
        if not isinstance(raw_questions, list):
            raw_questions = []
        normalized_questions = [
            " ".join(str(item or "").split()).strip()
            for item in raw_questions
            if " ".join(str(item or "").split()).strip()
        ]
        if not normalized_questions:
            normalized_questions = self._extract_important_questions_fallback()

        normalized_date = _normalize_ymd(payload.get("appointment_date"))
        if not normalized_date:
            normalized_date = _parse_corpus_date_fallback(corpus_text, self.artifacts.started_at)
        if not raw_name:
            raw_name = _parse_corpus_name_fallback(corpus_text)
        normalized_time = _normalize_hhmm(payload.get("appointment_time"))
        if not normalized_time:
            normalized_time = _parse_corpus_time_fallback(corpus_text)

        details = CriticalCallDetails(
            name=raw_name,
            age=age_value,
            appointment_date=normalized_date,
            appointment_time=normalized_time,
            important_questions_asked=normalized_questions[:10],
        )
        self.artifacts.recording_llm_details = details
        self.artifacts.metrics["recording_llm_extracted"] = 1.0

        if details.name:
            self.artifacts.memory.lead_name = details.name
            self.customer_name = details.name
        if details.age is not None:
            self.artifacts.memory.contact_details["age"] = str(details.age)
        if details.appointment_date and details.appointment_time:
            self.artifacts.memory.appointment_details = (
                f"Appointment noted for {details.appointment_date} at {details.appointment_time}"
            )
        elif details.appointment_date:
            self.artifacts.memory.appointment_details = f"Appointment date noted: {details.appointment_date}"
        elif details.appointment_time:
            self.artifacts.memory.appointment_details = f"Appointment time noted: {details.appointment_time}"

    def _booking_intent_detected_from_corpus(self, corpus_text: str) -> bool:
        if self.client.config.conversation_mode == "appointment_booking":
            return True
        text = " ".join(str(corpus_text or "").split()).strip().lower()
        if not text:
            return False
        booking_tokens = (
            "appointment",
            "booking",
            "book",
            "slot",
            "schedule",
            "अपॉइंटमेंट",
            "बुकिंग",
            "बुक",
            "स्लॉट",
            "अपॉइंटमेंट",
            "अपॉईंटमेंट",
            "बुक कर",
            "अपॉइंट",
        )
        return any(token in text for token in booking_tokens)

    def _save_input_audio_capture(self) -> None:
        if not self._input_audio_capture and not self._agent_audio_capture and not self._conversation_audio_capture:
            return

        if self._input_audio_capture:
            caller_wav_path = self.session_dir / "caller_audio.wav"
            with wave.open(str(caller_wav_path), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(16_000)
                wav_file.writeframes(bytes(self._input_audio_capture))
            caller_average_energy = (
                self._input_audio_avg_total / self._input_audio_avg_samples if self._input_audio_avg_samples else 0.0
            )
            self.artifacts.metrics["caller_audio_peak_amplitude"] = float(self._input_audio_peak)
            self.artifacts.metrics["caller_audio_avg_amplitude"] = float(round(caller_average_energy, 3))
            self.artifacts.metrics["caller_audio_seconds"] = round(len(self._input_audio_capture) / (16_000 * 2), 3)
            logger.info(
                "Saved caller audio capture: path=%s seconds=%.3f peak=%s avg=%.3f",
                caller_wav_path,
                self.artifacts.metrics["caller_audio_seconds"],
                self._input_audio_peak,
                caller_average_energy,
            )

        if self._agent_audio_capture:
            agent_wav_path = self.session_dir / "agent_audio.wav"
            with wave.open(str(agent_wav_path), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(24_000)
                wav_file.writeframes(bytes(self._agent_audio_capture))
            agent_average_energy = (
                self._agent_audio_avg_total / self._agent_audio_avg_samples if self._agent_audio_avg_samples else 0.0
            )
            self.artifacts.metrics["agent_audio_peak_amplitude"] = float(self._agent_audio_peak)
            self.artifacts.metrics["agent_audio_avg_amplitude"] = float(round(agent_average_energy, 3))
            self.artifacts.metrics["agent_audio_seconds"] = round(len(self._agent_audio_capture) / (24_000 * 2), 3)
            logger.info(
                "Saved agent audio capture: path=%s seconds=%.3f peak=%s avg=%.3f",
                agent_wav_path,
                self.artifacts.metrics["agent_audio_seconds"],
                self._agent_audio_peak,
                agent_average_energy,
            )

        if self._conversation_audio_capture:
            conversation_wav_path = self.session_dir / "conversation_audio.wav"
            with wave.open(str(conversation_wav_path), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(16_000)
                wav_file.writeframes(bytes(self._conversation_audio_capture))
            self.artifacts.metrics["conversation_audio_seconds"] = round(
                len(self._conversation_audio_capture) / (16_000 * 2),
                3,
            )
            logger.info(
                "Saved full conversation audio capture: path=%s seconds=%.3f",
                conversation_wav_path,
                self.artifacts.metrics["conversation_audio_seconds"],
            )

    def _should_run_faster_stt_fallback(self) -> bool:
        if not self.faster_stt.enabled:
            return False
        if float(self.artifacts.metrics.get("parallel_stt_chars") or 0.0) > 0.0:
            return False
        if not (self._is_exotel_session() or self._is_twilio_session()):
            return False
        audio_seconds = float(self.artifacts.metrics.get("caller_audio_seconds") or 0.0)
        if audio_seconds < 1.2:
            return False
        user_turns = [turn for turn in self.artifacts.transcript if turn.speaker == "user" and str(turn.text or "").strip()]
        joined = " ".join(" ".join(str(turn.text or "").split()).strip() for turn in user_turns).strip()
        if not joined:
            return True
        if len(joined) < 35:
            return True
        if not self._sanitize_captured_lead_name(self.artifacts.memory.lead_name):
            return True
        details = " ".join(
            str(item or "")
            for item in (
                self.artifacts.memory.appointment_details,
                self.artifacts.memory.next_step,
                (self.artifacts.summary.summary if self.artifacts.summary else ""),
            )
        ).lower()
        has_schedule_signal = any(
            token in details
            for token in (
                "appointment",
                "slot",
                "tomorrow",
                "day after",
                "उद्या",
                "परवा",
                "कल",
                "परसों",
                "रविवार",
                "सोमवार",
                "मंगळवार",
                "बजे",
                "वाजता",
            )
        )
        return not has_schedule_signal

    async def _augment_transcript_with_faster_stt_fallback(self) -> None:
        if not self._should_run_faster_stt_fallback():
            return
        wav_path = self.session_dir / "caller_audio.wav"
        if not wav_path.exists():
            return
        stt_text = " ".join(
            (await self.faster_stt.transcribe_wav_async(wav_path, self.current_language)).split()
        ).strip()
        if not stt_text:
            return

        existing_user_text = " ".join(
            " ".join(str(turn.text or "").split()).strip()
            for turn in self.artifacts.transcript
            if turn.speaker == "user" and str(turn.text or "").strip()
        ).strip().lower()
        normalized_stt = stt_text.lower()
        if existing_user_text and (normalized_stt in existing_user_text or existing_user_text in normalized_stt):
            return

        self._turn_index += 1
        turn = TranscriptTurn(speaker="user", text=stt_text, turn_id=self._turn_index)
        self.session_logger.append_turn(self.artifacts, turn)
        intents = tuple(self._classify_user_intents(stt_text))
        self._update_lead_identity_from_user_text(stt_text, intents)
        self.artifacts.memory = update_memory_from_user_text(self.artifacts.memory, stt_text)
        self.artifacts.metrics["faster_stt_augmented"] = 1.0
        self.artifacts.metrics["faster_stt_chars"] = float(len(stt_text))
        self.artifacts.metrics["stt_hybrid_mode_active"] = 1.0
        logger.info(
            "Augmented transcript via Faster-Whisper fallback: session_id=%s chars=%s",
            self.session_id,
            len(stt_text),
        )

    async def _begin_shutdown(self) -> None:
        """Close live and audio resources once so blocked loops can exit promptly."""
        async with self._shutdown_lock:
            if self._shutdown_started:
                return
            self._shutdown_started = True
            self._cancel_early_commit_task()

            close_errors: list[str] = []
            for label, closer in (("audio", self.audio.close), ("live", self.live.close)):
                try:
                    await closer()
                except Exception as exc:
                    logger.warning("Failed while closing %s during shutdown: %s", label, exc)
                    close_errors.append(f"{label} shutdown failed: {exc}")
            self._running = False
            if close_errors:
                self.artifacts.errors.extend(close_errors)

    async def _await_shutdown_task(self, task: asyncio.Task[None]) -> None:
        """Treat normal close races from background tasks as clean shutdowns."""
        try:
            await task
        except asyncio.CancelledError:
            return
        except Exception as exc:
            if self._is_clean_disconnect(exc):
                logger.debug("Suppressed clean disconnect while awaiting shutdown task: %s", exc)
                return
            raise

    def _schedule_auto_stop(self) -> None:
        if self._auto_stop_task is not None and not self._auto_stop_task.done():
            return
        with contextlib.suppress(RuntimeError):
            loop = asyncio.get_running_loop()
            self._auto_stop_task = loop.create_task(self._stop_after_playback())

    def _schedule_short_response_commit(self, text: str, is_final: bool) -> None:
        if not is_final:
            self._cancel_early_commit_task()
            return
        if self.client.config.conversation_mode == "appointment_booking":
            # Let the caller finish naturally in booking calls; avoid cutting short utterances.
            self._cancel_early_commit_task()
            return
        normalized = " ".join(text.split()).strip()
        if self._is_exotel_session() and self._is_first_user_reply_greeting_only(normalized):
            self._cancel_early_commit_task()
            return
        if not self._is_short_response_candidate(normalized):
            self._cancel_early_commit_task()
            return
        with contextlib.suppress(RuntimeError):
            loop = asyncio.get_running_loop()
            self._cancel_early_commit_task()
            self._early_commit_task = loop.create_task(self._commit_short_response_after_delay(normalized))

    async def _commit_short_response_after_delay(self, expected_text: str) -> None:
        try:
            await asyncio.sleep(self._short_response_commit_seconds())
            if not self._running:
                return
            pending = self._partial_turns.get("user", "").strip()
            if pending != expected_text:
                return
            if self._turn_buffers.get("agent"):
                return
            logger.debug("Early-committing short final user reply: %s", expected_text)
            await self._signal_live_activity_end()
            await self._commit_turns(None)
            if self._running:
                self._set_status("listening", "Ready for the next turn.")
        except asyncio.CancelledError:
            return

    async def _partial_commit_watchdog(self) -> None:
        try:
            while self._running:
                await asyncio.sleep(0.05)
                if not self._should_force_commit_partial_user_turn():
                    continue
                pending = self._partial_turns.get("user", "").strip() or self._latest_user_snippet.strip()
                if not pending:
                    continue
                logger.debug("Force-committing Exotel partial user reply after silence: %s", pending)
                self._partial_turns["user"] = pending
                await self._signal_live_activity_end()
                await self._commit_turns(None)
                if self._running:
                    self._set_status("listening", "Ready for the next turn.")
        except asyncio.CancelledError:
            return

    async def _low_confidence_reprompt_watchdog(self) -> None:
        try:
            while self._running:
                await asyncio.sleep(0.05)
                if self._should_infer_binary_confirmation_from_audio():
                    self._binary_confirmation_resolved = True
                    logger.debug("Inferring affirmative reply from short voiced burst after binary question.")
                    self._schedule_deterministic_followup("audio-confirmed", ("affirm", "short_reply", "audio_inferred"))
                    continue
                if not self._should_reprompt_after_low_confidence_speech():
                    continue
                self._last_low_confidence_reprompt_at = monotonic()
                logger.debug("Re-prompting after low-confidence telephony speech with no usable transcript.")
                await self._deliver_native_agent_line(self._render_low_confidence_reprompt())
        except asyncio.CancelledError:
            return

    async def _silence_followup_watchdog(self) -> None:
        try:
            while self._running:
                await asyncio.sleep(0.1)
                if not self._should_send_guest_silence_followup():
                    continue
                self._silence_follow_up_sent = True
                await self._deliver_native_agent_line(self._render_silence_follow_up_text())
        except asyncio.CancelledError:
            return

    def _cancel_early_commit_task(self) -> None:
        if self._early_commit_task is not None and not self._early_commit_task.done():
            self._early_commit_task.cancel()
        self._early_commit_task = None

    def _schedule_deterministic_followup(self, user_text: str, intents: tuple[str, ...]) -> None:
        if self._scripted_flow_stage == "closing_confirmation":
            return
        if self.client.config.conversation_mode == "appointment_booking":
            actionable_intents = set(intents) - {"short_reply", "greeting"}
            if not actionable_intents:
                return
        followup = self._plan_deterministic_followup(user_text, intents)
        if not followup:
            return
        if self._deterministic_followup_task is not None and not self._deterministic_followup_task.done():
            self._deterministic_followup_task.cancel()
        with contextlib.suppress(RuntimeError):
            loop = asyncio.get_running_loop()
            self._deterministic_followup_task = loop.create_task(
                self._deliver_deterministic_followup_after_delay(followup)
            )

    async def _deliver_deterministic_followup_after_delay(self, text: str) -> None:
        try:
            delay_seconds = (
                0.25
                if self._is_piopiy_session()
                else (
                    APPOINTMENT_DETERMINISTIC_FOLLOWUP_DELAY_SECONDS
                    if self.client.config.conversation_mode == "appointment_booking"
                    else DEFAULT_DETERMINISTIC_FOLLOWUP_DELAY_SECONDS
                )
            )
            await asyncio.sleep(delay_seconds)
            if not self._running:
                return
            if self.audio.is_playing():
                return
            if self._pending_native_line is not None or self._native_line_in_flight:
                return
            if self._turn_buffers.get("agent") or self._partial_turns.get("agent"):
                if self.client.config.conversation_mode == "appointment_booking":
                    # Prefer deterministic next-step prompt for low-latency booking turns.
                    self._turn_buffers.pop("agent", None)
                    self._partial_turns.pop("agent", None)
                else:
                    return
            if self._partial_turns.get("user"):
                return
            await self._deliver_native_agent_line(text)
            if self._scripted_flow_stage == "closing_confirmation":
                self._awaiting_binary_confirmation = False
                self._binary_confirmation_resolved = True
                self._schedule_auto_stop()
        except asyncio.CancelledError:
            return

    async def _signal_live_activity_end(self) -> None:
        if not self._should_use_explicit_vad():
            return
        try:
            await self.live.signal_activity_end()
        except Exception:
            logger.debug("Failed to signal Gemini live activity end for telephony turn.", exc_info=True)

    def _is_exotel_session(self) -> bool:
        return isinstance(self.telephony_context, dict) and self.telephony_context.get("provider") == "exotel"

    def _is_twilio_session(self) -> bool:
        return isinstance(self.telephony_context, dict) and self.telephony_context.get("provider") == "twilio"

    def _is_meta_whatsapp_session(self) -> bool:
        return isinstance(self.telephony_context, dict) and self.telephony_context.get("provider") == "meta_whatsapp"

    def _is_piopiy_session(self) -> bool:
        return isinstance(self.telephony_context, dict) and self.telephony_context.get("provider") == "piopiy"

    def _is_browser_session(self) -> bool:
        return isinstance(self.telephony_context, dict) and self.telephony_context.get("provider") == "browser"

    def _is_partial_commit_session(self) -> bool:
        return (
            self._is_exotel_session()
            or self._is_meta_whatsapp_session()
            or self._is_twilio_session()
            or self._is_piopiy_session()
            or self._is_browser_session()
        )

    def _should_use_explicit_vad(self) -> bool:
        if self._is_exotel_session():
            return self.settings.exotel_use_explicit_vad
        return (
            self._is_twilio_session()
            or self._is_meta_whatsapp_session()
            or self._is_piopiy_session()
            or self._is_browser_session()
        )

    def _plan_deterministic_followup(self, user_text: str, intents: tuple[str, ...]) -> str | None:
        if not self._is_fast_turn_mode():
            return None
        normalized = user_text.lower().strip(" .,!?:;")
        # For appointment booking, allow deterministic stage progression even on longer replies.
        if (
            self.client.config.conversation_mode != "appointment_booking"
            and "short_reply" not in intents
            and len(normalized.split()) > 3
        ):
            return None
        project_type = self.project.project_type if self.project is not None else "custom"
        if self._scripted_flow_stage == "none":
            if self.client.config.conversation_mode == "appointment_booking":
                self._scripted_flow_stage = "identity_confirmation"
            else:
                self._scripted_flow_stage = "opening_confirmation"

        if project_type == "appointment_booking" or self.client.config.conversation_mode == "appointment_booking":
            return self._plan_appointment_booking_followup(user_text, intents)
        if project_type in {"lead_qualification", "followup", "sales"}:
            return self._plan_generic_project_followup(project_type, intents)
        return None

    def _plan_appointment_booking_followup(self, user_text: str, intents: tuple[str, ...]) -> str | None:
        if self._scripted_flow_stage == "identity_confirmation":
            if "affirm" in intents:
                self._scripted_flow_stage = "collect_age"
                if self.current_language == "marathi":
                    return "धन्यवाद. तुमचं वय किती आहे?"
                if self.current_language == "hindi":
                    return "धन्यवाद। आपकी उम्र कितनी है?"
                return "Thank you. What is the age?"
            if "deny" in intents:
                self._scripted_flow_stage = "identity_redirect"
                if self.current_language == "marathi":
                    return "ठीक आहे. मग कृपया सांगा, मी कोणाशी बोलत आहे?"
                if self.current_language == "hindi":
                    return "ठीक है। फिर कृपया बताइए, मैं किससे बात कर रही हूँ?"
                return "Alright. Please tell me who I am speaking with."
            return None

        if self._scripted_flow_stage == "identity_redirect":
            if "provided_name" in intents or self._looks_like_name_reply(user_text, intents):
                self._scripted_flow_stage = "collect_age"
                if self.current_language == "marathi":
                    return "धन्यवाद. तुमचं वय किती आहे?"
                if self.current_language == "hindi":
                    return "धन्यवाद। आपकी उम्र कितनी है?"
                return "Thank you. What is the age?"
            return None

        if self._scripted_flow_stage in {"interest_confirmation", "opening_confirmation"}:
            if "affirm" in intents:
                self._scripted_flow_stage = "collect_age"
                if self.current_language == "marathi":
                    return "धन्यवाद. तुमचं वय किती आहे?"
                if self.current_language == "hindi":
                    return "धन्यवाद। आपकी उम्र कितनी है?"
                return "Thank you. What is the age?"
            if "deny" in intents:
                self._scripted_flow_stage = "closing_confirmation"
                if self.current_language == "marathi":
                    return "ठीक आहे. तुम्हाला आत्ता हार्ट चेकअपमध्ये रस नाही, ते नोंदवलं आहे. धन्यवाद."
                if self.current_language == "hindi":
                    return "ठीक है। अभी आपकी हार्ट चेकअप में रुचि नहीं है, यह नोट कर लिया है। धन्यवाद।"
                return "Alright. Noted that you are not interested in a heart checkup right now. Thank you."
            return None

        if self._scripted_flow_stage == "collect_name":
            if "provided_name" in intents or self._looks_like_name_reply(user_text, intents):
                self._scripted_flow_stage = "collect_age"
                if self.current_language == "marathi":
                    return "धन्यवाद. वय किती आहे?"
                if self.current_language == "hindi":
                    return "धन्यवाद। उम्र कितनी है?"
                return "Thank you. What is the age?"
            return None

        if self._scripted_flow_stage == "collect_age":
            if "provided_age" in intents:
                self._scripted_flow_stage = "collect_chest_pain"
                if self.current_language == "marathi":
                    return "सध्या छातीत दुखणे, जडपणा किंवा दाब जाणवतो का? हो की नाही?"
                if self.current_language == "hindi":
                    return "अभी सीने में दर्द, भारीपन या दबाव है क्या? हाँ या नहीं?"
                return "Do you currently have chest pain, heaviness, or pressure? Yes or no?"
            return None

        if self._scripted_flow_stage == "collect_chest_pain":
            if {"chest_pain_yes", "chest_pain_no", "affirm", "deny"} & set(intents):
                self._scripted_flow_stage = "collect_family_history"
                if self.current_language == "marathi":
                    return "कुटुंबात आधी हृदयविकाराचा इतिहास आहे का? हो की नाही?"
                if self.current_language == "hindi":
                    return "परिवार में पहले से हार्ट डिजीज का इतिहास है क्या? हाँ या नहीं?"
                return "Is there any previous history of heart disease in the family? Yes or no?"
            return None

        if self._scripted_flow_stage == "collect_family_history":
            if {"family_history_yes", "family_history_no", "affirm", "deny"} & set(intents):
                self._scripted_flow_stage = "schedule_preference"
                if self.current_language == "marathi":
                    return "ठीक आहे. अपॉइंटमेंटसाठी उद्या की परवा जास्त सोयीचं आहे?"
                if self.current_language == "hindi":
                    return "ठीक है। अपॉइंटमेंट के लिए कल या परसों में क्या सुविधाजनक है?"
                return "Alright. For appointment, does tomorrow or the day after work better?"
            return None

        if self._scripted_flow_stage == "schedule_preference":
            if "date_tomorrow" in intents:
                self._scripted_flow_stage = "time_window"
                self._set_appointment_booking_snapshot(date_value=self._resolve_relative_date_value("tomorrow"))
                if self.current_language == "marathi":
                    return f"ठीक आहे. {self._appointment_date_value} साठी सकाळ, दुपार की संध्याकाळ कोणती वेळ सोयीची?"
                if self.current_language == "hindi":
                    return f"ठीक है। {self._appointment_date_value} के लिए सुबह, दोपहर या शाम में कौन सा समय ठीक रहेगा?"
                return f"Great. For {self._appointment_date_value}, what time works best: morning, afternoon, or evening?"
            if "date_day_after" in intents:
                self._scripted_flow_stage = "time_window"
                self._set_appointment_booking_snapshot(date_value=self._resolve_relative_date_value("day_after"))
                if self.current_language == "marathi":
                    return f"ठीक आहे. {self._appointment_date_value} साठी सकाळ, दुपार की संध्याकाळ कोणती वेळ सोयीची?"
                if self.current_language == "hindi":
                    return f"ठीक है। {self._appointment_date_value} के लिए सुबह, दोपहर या शाम में कौन सा समय ठीक रहेगा?"
                return f"Great. For {self._appointment_date_value}, what time works best: morning, afternoon, or evening?"
            if "affirm" in intents:
                self._scripted_flow_stage = "time_window"
                self._set_appointment_booking_snapshot(date_value=self._resolve_relative_date_value("tomorrow"))
                if self.current_language == "marathi":
                    return f"ठीक आहे. {self._appointment_date_value} साठी सकाळ, दुपार की संध्याकाळ यात कोणती वेळ सोयीची आहे?"
                if self.current_language == "hindi":
                    return f"ठीक है। {self._appointment_date_value} के लिए सुबह, दोपहर या शाम में कौन सा समय ठीक रहेगा?"
                return f"Okay. For {self._appointment_date_value}, which time suits you better: morning, afternoon, or evening?"
            if "deny" in intents:
                self._scripted_flow_stage = "callback_preference"
                if self.current_language == "marathi":
                    return "ठीक आहे. मग आमच्या टीमकडून नंतर कॉलबॅक हवा आहे का?"
                if self.current_language == "hindi":
                    return "ठीक है। क्या आप चाहेंगे कि हमारी टीम बाद में आपको कॉलबैक करे?"
                return "Alright. Would you like a callback later from our team?"
            return None

        if self._scripted_flow_stage == "time_window":
            if {"time_morning", "time_afternoon", "time_evening"} & set(intents):
                self._scripted_flow_stage = "closing_confirmation"
                self._set_appointment_booking_snapshot(time_value=self._resolve_time_value_from_intents(intents))
                date_value = self._appointment_date_value or self._resolve_relative_date_value("tomorrow")
                if self.current_language == "marathi":
                    return f"ठीक आहे. तुमची अपॉइंटमेंट {date_value} रोजी {self._appointment_time_value} ला नोंदवली आहे. व्हॉट्सॲपवर पत्ता आणि सूचना पाठवते. धन्यवाद."
                if self.current_language == "hindi":
                    return f"ठीक है। आपकी अपॉइंटमेंट {date_value} को {self._appointment_time_value} पर नोट कर ली है। व्हॉट्सऐप पर पता और निर्देश भेज रही हूँ। धन्यवाद।"
                return f"Great. Your appointment is noted for {date_value} at {self._appointment_time_value}. I will share address and pre-checkup instructions on WhatsApp. Thank you."
            if "affirm" in intents:
                self._scripted_flow_stage = "closing_confirmation"
                self._set_appointment_booking_snapshot(time_value=self._appointment_time_value or "11:00 AM")
                date_value = self._appointment_date_value or self._resolve_relative_date_value("tomorrow")
                if self.current_language == "marathi":
                    return f"ठीक आहे. अपॉइंटमेंट {date_value} रोजी {self._appointment_time_value} ला नोंदवली आहे. व्हॉट्सॲपवर पत्ता आणि सूचना पाठवते. धन्यवाद."
                if self.current_language == "hindi":
                    return f"ठीक है। अपॉइंटमेंट {date_value} को {self._appointment_time_value} पर नोट कर ली है। व्हॉट्सऐप पर पता और निर्देश भेज रही हूँ। धन्यवाद।"
                return f"Noted. Appointment is set for {date_value} at {self._appointment_time_value}. I will share address and pre-checkup instructions on WhatsApp. Thank you."
            if "deny" in intents:
                self._scripted_flow_stage = "callback_preference"
                if self.current_language == "marathi":
                    return "ठीक आहे. मग तुम्हाला नंतर कॉलबॅक हवा आहे का?"
                if self.current_language == "hindi":
                    return "ठीक है। क्या आपको बाद में कॉलबैक चाहिए?"
                return "Alright. Would you like a callback later?"
            return None

        if self._scripted_flow_stage == "callback_preference":
            if "affirm" in intents:
                self._scripted_flow_stage = "closing_confirmation"
                if self.current_language == "marathi":
                    return "ठीक आहे. तुमची विचारणा नोंदवली आहे. आमची टीम लवकरच कॉलबॅक करेल. धन्यवाद."
                if self.current_language == "hindi":
                    return "ठीक है। आपकी enquiry नोट हो गई है। हमारी टीम जल्दी कॉलबैक करेगी। धन्यवाद।"
                return "Alright. Your enquiry is noted. Our team will call you back shortly. Thank you."
            if "deny" in intents:
                self._scripted_flow_stage = "closing_confirmation"
                if self.current_language == "marathi":
                    return "ठीक आहे. तुमची विचारणा नोंदवली आहे. धन्यवाद."
                if self.current_language == "hindi":
                    return "ठीक है। आपकी enquiry नोट कर ली गई है। धन्यवाद।"
                return "Understood. We have noted your enquiry. Thank you."
            return None
        return None

    def _looks_like_name_reply(self, user_text: str, intents: tuple[str, ...]) -> bool:
        if "provided_name" in intents:
            return True
        lowered = " ".join((user_text or "").lower().split()).strip(" .,!?:;")
        if not lowered:
            return False
        if any(tag in intents for tag in ("affirm", "deny", "date_tomorrow", "date_day_after", "time_morning", "time_afternoon", "time_evening")):
            return False
        return len(lowered.split()) <= 4

    def _sanitize_captured_lead_name(self, value: str | None) -> str | None:
        raw = " ".join(str(value or "").split()).strip(" .,!?:;")
        if not raw:
            return None
        lowered = raw.lower()
        if len(lowered) < 3:
            return None
        if len(raw.split()) > 4:
            return None
        if lowered in {"unknown", "unknown lead", "inbound caller", "customer", "caller", "prospect"}:
            return None
        if lowered in {"yes", "yeah", "yep", "no", "ok", "okay", "haan", "han", "ha", "ho", "ji", "jee", "speaking"}:
            return None
        if any(
            token in lowered
            for token in (
                "hospital",
                "helpdesk",
                "helpline",
                "clinic",
                "caller",
                "agent",
                "inbound",
                "outbound",
                "स्वामी",
                "हॉस्पिटल",
                "हेल्पडेस्क",
            )
        ):
            return None
        if re.search(r"\d", raw):
            return None
        return raw

    def _extract_name_from_user_text(self, user_text: str, intents: tuple[str, ...]) -> str | None:
        text = " ".join((user_text or "").split()).strip()
        if not text:
            return None

        patterns = [
            re.compile(r"\b(?:my name is|i am|i'm|this is)\s+([A-Za-z][A-Za-z' -]{1,50})\b", re.IGNORECASE),
            re.compile(r"(?:मेरा नाम|माझं नाव|माझे नाव)\s+([^\d.,!?]{2,50})", re.IGNORECASE),
            re.compile(r"\b(?:mai|main|me)\s+([A-Za-z][A-Za-z' -]{1,40})\s+(?:bol raha|bol rahi|speaking)\b", re.IGNORECASE),
            re.compile(r"\b([A-Za-z][A-Za-z' -]{1,40})\s+(?:speaking|here)\b", re.IGNORECASE),
        ]
        for pattern in patterns:
            match = pattern.search(text)
            if not match:
                continue
            candidate = self._sanitize_captured_lead_name(match.group(1))
            if candidate:
                return candidate

        if "provided_name" in intents or self._scripted_flow_stage in {
            "collect_name",
            "identity_redirect",
            "identity_confirmation",
            "opening_confirmation",
        }:
            lowered = text.lower().strip(" .,!?:;")
            if lowered in {
                "speaking",
                "yes",
                "yeah",
                "yep",
                "haan",
                "han",
                "ha",
                "ho",
                "ji",
                "jee",
                "nahi",
                "no",
            }:
                return None
            if len(lowered.split()) <= 4:
                return self._sanitize_captured_lead_name(text)
        return None

    def _update_lead_identity_from_user_text(self, user_text: str, intents: tuple[str, ...]) -> None:
        candidate = self._extract_name_from_user_text(user_text, intents)
        if not candidate:
            return
        self.artifacts.memory.lead_name = candidate
        self.customer_name = candidate

    def _resolve_relative_date_value(self, relative: str) -> str:
        now = datetime.now()
        if relative == "day_after":
            target = now + timedelta(days=2)
        else:
            target = now + timedelta(days=1)
        return target.strftime("%d %b %Y")

    def _resolve_time_value_from_intents(self, intents: tuple[str, ...]) -> str:
        if "time_morning" in intents:
            return "10:00 AM"
        if "time_afternoon" in intents:
            return "2:00 PM"
        if "time_evening" in intents:
            return "6:00 PM"
        return "11:00 AM"

    def _set_appointment_booking_snapshot(self, *, date_value: str | None = None, time_value: str | None = None) -> None:
        if date_value:
            self._appointment_date_value = date_value
        if time_value:
            self._appointment_time_value = time_value
        if self._appointment_date_value and self._appointment_time_value:
            self.artifacts.memory.appointment_details = (
                f"Appointment noted for {self._appointment_date_value} at {self._appointment_time_value}"
            )
            return
        if self._appointment_date_value:
            self.artifacts.memory.appointment_details = f"Appointment date noted: {self._appointment_date_value}"
            return
        if self._appointment_time_value:
            self.artifacts.memory.appointment_details = f"Appointment time noted: {self._appointment_time_value}"

    def _plan_generic_project_followup(self, project_type: str, intents: tuple[str, ...]) -> str | None:
        if (
            "affirm" not in intents
            and "deny" not in intents
            and "callback_request" not in intents
            and "date_tomorrow" not in intents
            and "date_day_after" not in intents
            and "time_morning" not in intents
            and "time_afternoon" not in intents
            and "time_evening" not in intents
        ):
            return None
        if project_type == "lead_qualification":
            if "callback_request" in intents:
                if self.current_language == "marathi":
                    return "ठीक आहे. कृपया सांगा, उद्या की परवा तुम्हाला कॉलबॅक सोयीचा आहे?"
                if self.current_language == "hindi":
                    return "ठीक है। कृपया बताइए, आपको कल कॉलबैक चाहिए या परसों?"
                return "Got it. Would you prefer a callback tomorrow or the day after?"
            if "date_tomorrow" in intents or "date_day_after" in intents:
                if self.current_language == "marathi":
                    return "नोंद केली. सकाळ, दुपार की संध्याकाळ कोणती वेळ योग्य आहे?"
                if self.current_language == "hindi":
                    return "नोट कर लिया। सुबह, दोपहर या शाम में कौन सा समय ठीक रहेगा?"
                return "Noted. Which time works best: morning, afternoon, or evening?"
            if {"time_morning", "time_afternoon", "time_evening"} & set(intents):
                if self.current_language == "marathi":
                    return "छान. आमची टीम त्या वेळेत तुमच्याशी संपर्क करेल."
                if self.current_language == "hindi":
                    return "ठीक है। हमारी टीम उसी समय आपसे संपर्क करेगी।"
                return "Great. Our team will contact you in that time window."
            if "affirm" in intents:
                if self.current_language == "marathi":
                    return "ठीक आहे. कृपया सांगा, तुम्हाला कोणत्या गोष्टीसाठी मदत हवी आहे?"
                if self.current_language == "hindi":
                    return "ठीक है। कृपया बताइए, आपको किस तरह की मदद चाहिए?"
                return "Alright. Please tell me what kind of help you need."
            if self.current_language == "marathi":
                return "ठीक आहे. मग तुम्हाला नंतर कॉलबॅक हवा आहे का?"
            if self.current_language == "hindi":
                return "ठीक है। क्या आपको बाद में कॉलबैक चाहिए?"
            return "Alright. Would you like a callback later?"
        if project_type == "followup":
            if "callback_request" in intents:
                if self.current_language == "marathi":
                    return "ठीक आहे. उद्या की परवा कॉलबॅक ठेवू?"
                if self.current_language == "hindi":
                    return "ठीक है। क्या हम कल कॉलबैक करें या परसों?"
                return "Sure. Should we schedule a callback tomorrow or the day after?"
            if "date_tomorrow" in intents or "date_day_after" in intents:
                if self.current_language == "marathi":
                    return "ठीक आहे. सकाळ, दुपार की संध्याकाळ यात कोणती वेळ योग्य आहे?"
                if self.current_language == "hindi":
                    return "ठीक है। सुबह, दोपहर या शाम में कौन सा समय सही रहेगा?"
                return "Perfect. What time suits you: morning, afternoon, or evening?"
            if {"time_morning", "time_afternoon", "time_evening"} & set(intents):
                if self.current_language == "marathi":
                    return "नोंद केली. त्या वेळेत फॉलो-अप कॉल करू."
                if self.current_language == "hindi":
                    return "नोट कर लिया। उसी समय फॉलो-अप कॉल करेंगे।"
                return "Noted. We will follow up in that time window."
            if "affirm" in intents:
                if self.current_language == "marathi":
                    return "छान. आपण मागच्या चर्चेपासून पुढे जाऊ या. तुम्हाला अजूनही यात रस आहे का?"
                if self.current_language == "hindi":
                    return "अच्छा। चलिए पिछली बात आगे बढ़ाते हैं। क्या आपकी अभी भी इसमें रुचि है?"
                return "Great. Let us continue from where we left off. Are you still interested?"
            if self.current_language == "marathi":
                return "ठीक आहे. मग तुम्हाला नंतर संपर्क करावा का?"
            if self.current_language == "hindi":
                return "ठीक है। क्या हम आपसे बाद में संपर्क करें?"
            return "Alright. Should we contact you later?"
        if project_type == "sales":
            if "callback_request" in intents:
                if self.current_language == "marathi":
                    return "नक्की. कृपया सांगा, उद्या की परवा कोणता दिवस योग्य आहे?"
                if self.current_language == "hindi":
                    return "ज़रूर। कृपया बताइए, कल सही रहेगा या परसों?"
                return "Sure. Would tomorrow or the day after work better for a callback?"
            if "date_tomorrow" in intents or "date_day_after" in intents:
                if self.current_language == "marathi":
                    return "छान. सकाळ, दुपार की संध्याकाळ यात कोणती वेळ योग्य आहे?"
                if self.current_language == "hindi":
                    return "अच्छा। सुबह, दोपहर या शाम में कौन सा समय सही रहेगा?"
                return "Great. Which time works: morning, afternoon, or evening?"
            if {"time_morning", "time_afternoon", "time_evening"} & set(intents):
                if self.current_language == "marathi":
                    return "ठीक आहे, नोंद केली. त्या वेळेत छोटा कॉल ठेवू."
                if self.current_language == "hindi":
                    return "ठीक है, नोट कर लिया। उसी समय एक छोटा कॉल रखते हैं।"
                return "Noted. We will keep a short call in that window."
            if "affirm" in intents:
                if self.current_language == "marathi":
                    return "छान. मी थोडक्यात सांगते, मग पुढचं पाऊल ठरवू."
                if self.current_language == "hindi":
                    return "अच्छा। मैं संक्षेप में बताती हूँ, फिर अगला कदम तय करते हैं।"
                return "Great. Let me explain briefly, then we can decide the next step."
            if self.current_language == "marathi":
                return "ठीक आहे. मग तुम्हाला नंतर छोटा कॉलबॅक हवा आहे का?"
            if self.current_language == "hindi":
                return "ठीक है। क्या आप बाद में एक छोटा कॉलबैक चाहेंगे?"
            return "Alright. Would you like a brief callback later?"
        return None

    def _short_response_commit_seconds(self) -> float:
        if self._is_fast_turn_mode():
            return min(
                self.settings.exotel_short_response_commit_seconds
                if self._is_exotel_session()
                else SHORT_RESPONSE_COMMIT_SECONDS,
                FAST_TURN_SHORT_RESPONSE_COMMIT_SECONDS,
            )
        if self._is_twilio_session():
            return min(SHORT_RESPONSE_COMMIT_SECONDS, TWILIO_SHORT_RESPONSE_COMMIT_SECONDS)
        if self._is_exotel_session():
            return self.settings.exotel_short_response_commit_seconds
        return SHORT_RESPONSE_COMMIT_SECONDS

    def _interruption_grace_seconds(self) -> float:
        if self._is_exotel_session():
            return self.settings.exotel_interruption_grace_seconds
        return INTERRUPTION_GRACE_SECONDS

    def _barge_in_debounce_seconds(self) -> float:
        if self._is_exotel_session():
            return self.settings.exotel_barge_in_debounce_seconds
        return BARGE_IN_DEBOUNCE_SECONDS

    def _should_force_commit_partial_user_turn(self) -> bool:
        if (
            not self._running
            or not self._is_partial_commit_session()
            or (self._is_exotel_session() and not self.settings.exotel_enable_partial_commit_watchdog)
        ):
            return False
        if self._pending_native_line is not None or self._native_line_in_flight:
            return False
        if self.audio.is_playing():
            return False
        pending = self._partial_turns.get("user", "").strip() or self._latest_user_snippet.strip()
        if len(pending) < self.settings.exotel_min_partial_commit_characters:
            return False
        if pending == self._last_committed_user_text:
            return False
        if self._turn_buffers.get("agent"):
            return False
        if self._latest_user_snippet_at <= 0.0:
            return False
        silence_threshold = self._partial_commit_silence_seconds(pending)
        return (monotonic() - self._latest_user_snippet_at) >= silence_threshold

    def _should_reprompt_after_low_confidence_speech(self) -> bool:
        if (
            not self._running
            or not self._is_partial_commit_session()
            or not self.settings.exotel_enable_low_confidence_reprompt
        ):
            return False
        if self._pending_native_line is not None or self._native_line_in_flight:
            return False
        if self.audio.is_playing():
            return False
        if self._partial_turns.get("user") or self._turn_buffers.get("user"):
            return False
        if not self._opening_delivered or not self._waiting_for_user_after_agent:
            return False
        if self._last_user_audio_activity_at <= self._last_agent_turn_at:
            return False
        if self._last_user_audio_activity_at <= self._last_low_confidence_reprompt_at:
            return False
        silence_elapsed = monotonic() - self._last_user_audio_activity_at
        return silence_elapsed >= self.settings.exotel_low_confidence_reprompt_silence_seconds

    def _should_infer_binary_confirmation_from_audio(self) -> bool:
        if not self._running or not self._awaiting_binary_confirmation or self._binary_confirmation_resolved:
            return False
        if self.audio.is_playing():
            return False
        if self._pending_native_line is not None or self._native_line_in_flight:
            return False
        if self._partial_turns.get("user") or self._turn_buffers.get("user"):
            return False
        if self._last_user_audio_activity_at <= self._binary_confirmation_prompted_at:
            return False
        return (monotonic() - self._last_user_audio_activity_at) >= GENERIC_BINARY_CONFIRMATION_SILENCE_SECONDS

    def _binary_confirmation_audio_threshold(self) -> float:
        if self._is_exotel_session() or self._is_meta_whatsapp_session():
            return self.settings.exotel_low_confidence_audio_threshold
        return GENERIC_BINARY_CONFIRMATION_AUDIO_THRESHOLD

    def _partial_commit_silence_seconds(self, pending: str) -> float:
        if self._is_browser_session():
            if self._is_short_response_candidate(pending):
                return min(self.settings.exotel_short_reply_partial_commit_silence_seconds, 0.18)
            return min(self.settings.exotel_partial_commit_silence_seconds, 0.35)
        if self._is_twilio_session():
            if self._is_short_response_candidate(pending):
                return TWILIO_SHORT_REPLY_PARTIAL_COMMIT_SILENCE_SECONDS
            return TWILIO_PARTIAL_COMMIT_SILENCE_SECONDS
        if self._is_first_user_reply_greeting_only(pending):
            return EXOTEL_FIRST_REPLY_GREETING_HOLD_SECONDS
        if self._is_fast_turn_mode():
            if self._is_short_response_candidate(pending):
                return min(
                    self.settings.exotel_short_reply_partial_commit_silence_seconds,
                    FAST_TURN_SHORT_REPLY_PARTIAL_COMMIT_SILENCE_SECONDS,
                )
            return min(self.settings.exotel_partial_commit_silence_seconds, FAST_TURN_PARTIAL_COMMIT_SILENCE_SECONDS)
        if self._is_short_response_candidate(pending):
            return self.settings.exotel_short_reply_partial_commit_silence_seconds
        return self.settings.exotel_partial_commit_silence_seconds

    def _normalize_user_commit_text(self, fragments: list[str]) -> str:
        cleaned = [" ".join(fragment.split()).strip() for fragment in fragments if fragment and fragment.strip()]
        if not cleaned:
            return ""
        if self._is_first_user_reply() and len(cleaned) >= 2 and self._is_greeting_only(cleaned[0]):
            merged = ", ".join(part for part in cleaned[:2] if part)
            rest = cleaned[2:]
            return " ".join([merged, *rest]).strip()
        return " ".join(cleaned).strip()

    def _is_first_user_reply(self) -> bool:
        return not any(turn.speaker == "user" for turn in self.artifacts.transcript)

    def _is_fast_turn_mode(self) -> bool:
        if self._is_guest_demo_workspace():
            return False
        if self._is_piopiy_session():
            return True
        project_type = self.project.project_type if self.project is not None else "custom"
        return self.client.config.conversation_mode == "appointment_booking" or project_type in {
            "appointment_booking",
            "lead_qualification",
            "followup",
        }

    def _is_guest_demo_workspace(self) -> bool:
        client_id = str(self.client.config.client_id or "")
        tags = {str(tag).strip().lower() for tag in (self.client.config.identity.tags or [])}
        return client_id.startswith("user_guest") or client_id == "aivoicebot4u_guest_demo" or "guest_demo" in tags

    def _guest_demo_opening_language(self) -> str | None:
        if not self._is_guest_demo_workspace() or self.project is None:
            return None
        project_id = str(self.project.project_id or "").strip().lower()
        if project_id == "led_arts_marathi_demo":
            return "marathi"
        if project_id == "magnum_hospital_marathi_demo":
            return "marathi"
        if project_id == "janardan_swami_cancer_helpdesk_demo":
            return "marathi"
        if project_id == "car_dealer_hindi_demo":
            return "hindi"
        return "english"

    def _is_first_user_reply_greeting_only(self, text: str) -> bool:
        return self._is_first_user_reply() and self._is_greeting_only(text)

    @staticmethod
    def _is_greeting_only(text: str) -> bool:
        normalized = text.lower().strip(" .,!?:;")
        greetings = {
            "hello",
            "hi",
            "hii",
            "hey",
            "namaste",
            "namaskar",
            "नमस्ते",
            "नमस्कार",
            "hello ji",
        }
        return normalized in greetings

    def _classify_user_intents(self, text: str) -> list[str]:
        normalized = VoiceSalesSession._normalize_intent_text(text)
        intents: list[str] = []
        if VoiceSalesSession._is_greeting_only(text):
            intents.append("greeting")
        affirmatives = {
            "haan",
            "han",
            "haa",
            "ha",
            "ho",
            "hoy",
            "ho na",
            "hona",
            "hoi",
            "ho ji",
            "barobar",
            "chalel",
            "thik",
            "thik aahe",
            "theek hai",
            "hm",
            "hmm",
            "yes",
            "yeah",
            "yep",
            "ji",
            "jee",
            "speaking",
            "bol raha hoon",
            "bol rahi hoon",
            "main bol raha hoon",
            "main bol rahi hoon",
            "हो",
            "होय",
            "हो ना",
            "हं",
            "हाँ",
            "हां",
            "जी",
        }
        negatives = {
            "no",
            "nope",
            "nahi",
            "nahin",
            "nai",
            "nako",
            "nahi re",
            "nahi pahije",
            "na",
            "नाही",
            "नको",
            "ना",
        }
        if normalized in affirmatives:
            intents.append("affirm")
        if normalized in negatives:
            intents.append("deny")

        if (
            "my name is" in normalized
            or "mera naam" in normalized
            or "मेरा नाम" in text
            or "majha nav" in normalized
            or "माझं नाव" in text
            or "माझे नाव" in text
            or "मी " in text and " बोल" in text
        ):
            intents.append("provided_name")
        age_match = re.search(r"\b([1-9][0-9]{0,2})\b", normalized)
        if age_match and (
            "age" in normalized
            or "umar" in normalized
            or "वय" in text
            or "saal" in normalized
            or "years" in normalized
            or "year" in normalized
            or "yrs" in normalized
        ):
            intents.append("provided_age")

        chest_markers = (
            "chest pain" in normalized
            or "chest" in normalized
            or "सीने" in text
            or "छातीत" in text
            or "छाती" in text
        )
        family_heart_markers = (
            "family history" in normalized
            or "family" in normalized and "heart" in normalized
            or "परिवार" in text and "हार्ट" in text
            or "कुटुंब" in text and "हृदय" in text
        )
        if chest_markers:
            if normalized in affirmatives or "affirm" in intents:
                intents.append("chest_pain_yes")
            if normalized in negatives or "deny" in intents:
                intents.append("chest_pain_no")
        if family_heart_markers:
            if normalized in affirmatives or "affirm" in intents:
                intents.append("family_history_yes")
            if normalized in negatives or "deny" in intents:
                intents.append("family_history_no")

        date_tomorrow_tokens = {
            "tomorrow",
            "kal",
            "udya",
            "udhya",
            "udyaa",
            "उद्या",
            "उद्या.",
            "कल",
            "कल.",
        }
        date_day_after_tokens = {
            "day after tomorrow",
            "parwa",
            "parvaa",
            "parva",
            "parवा",
            "परवा",
            "परसो",
            "परसों",
        }
        time_morning_tokens = {
            "morning",
            "सकाळ",
            "सकाळी",
            "सुबह",
            "sakal",
            "sakali",
            "sakaali",
        }
        time_afternoon_tokens = {
            "afternoon",
            "दुपार",
            "दोपहर",
            "dupar",
            "dopar",
        }
        time_evening_tokens = {
            "evening",
            "सायंकाळ",
            "सायंकाळी",
            "शाम",
            "संध्याकाळ",
            "sandhyakal",
            "sandhyakali",
            "sayankal",
            "sayankali",
        }
        callback_tokens = {
            "callback",
            "call back",
            "later call",
            "पुन्हा कॉल",
            "नंतर कॉल",
            "कॉलबॅक",
            "बाद में कॉल",
            "फिर कॉल",
            "nantar call",
            "nanter call",
            "punha call",
            "parat call",
            "later kara",
            "call kara",
        }
        padded = f" {normalized} "
        if VoiceSalesSession._contains_any(
            padded, {" उद्या ", " कल ", " udya ", " udhya ", " udyaa ", " tomorrow "}
        ) or normalized in date_tomorrow_tokens:
            intents.append("date_tomorrow")
        if (
            normalized in date_day_after_tokens
            or VoiceSalesSession._contains_any(
                padded, {" परवा ", " parwa ", " parava ", " parvaa ", " parva ", " परसों ", " परसो ", " day after tomorrow "}
            )
        ):
            intents.append("date_day_after")
        if normalized in time_morning_tokens or VoiceSalesSession._contains_any(
            padded, {" सकाळ ", " सुबह ", " morning ", " sakal ", " sakali ", " am "}
        ):
            intents.append("time_morning")
        if normalized in time_afternoon_tokens or VoiceSalesSession._contains_any(
            padded, {" दुपार ", " दोपहर ", " afternoon ", " dupar ", " dopar "}
        ):
            intents.append("time_afternoon")
        if normalized in time_evening_tokens or VoiceSalesSession._contains_any(
            padded, {" सायंकाळ ", " संध्याकाळ ", " शाम ", " evening ", " sayankal ", " sandhyakal ", " pm "}
        ):
            intents.append("time_evening")
        if normalized in callback_tokens or " callback" in normalized or "कॉलबॅक" in text or VoiceSalesSession._contains_any(padded, {" call me ", " call later ", " later call "}):
            intents.append("callback_request")
        if VoiceSalesSession._contains_any(
            padded, {" busy ", " not now ", " later ", " nantar ", " nanter ", " नंतर ", " बाद में ", " अभी नहीं "}
        ):
            intents.append("defer")
        if VoiceSalesSession._contains_any(
            padded, {" repeat ", " again ", " punha ", " parat sanga ", " पुन्हा ", " दोबारा "}
        ) or "समझा नहीं" in text or "samaj" in normalized:
            intents.append("repeat_request")
        if not VoiceSalesSession._looks_like_question(text):
            closing_markers = {
                "thank you",
                "thanks",
                "thankyou",
                "bye",
                "goodbye",
                "talk to you",
                "follow up",
                "follow-up",
                "appointment confirmed",
                "your appointment is confirmed",
                "your enquiry has been received",
                "enquiry has been received",
            }
            closing_native_markers = {
                "धन्यवाद",
                "शुक्रिया",
                "भेटू",
                "पुन्हा बोलू",
                "कॉल ठेवतो",
                "नमस्कार",
                "अपॉइंटमेंट कन्फर्म",
                "आपकी appointment",
                "आपकी enquiry",
                "तुमची appointment",
                "तुमची enquiry",
            }
            if any(marker in normalized for marker in closing_markers) or any(
                marker in text for marker in closing_native_markers
            ):
                intents.append("closing")

        if len(normalized.split()) <= 3:
            intents.append("short_reply")
        merged_catalog = self._merged_intent_phrase_catalog()
        padded = f" {normalized} "
        for intent_name, phrases in merged_catalog.items():
            for phrase in phrases:
                if VoiceSalesSession._matches_intent_phrase(text, normalized, padded, phrase):
                    intents.append(intent_name)
                    break
        return list(dict.fromkeys(intents))

    @staticmethod
    def _base_intent_phrase_catalog() -> dict[str, list[str]]:
        return {
            "confirm_identity": [
                "yes speaking",
                "this is",
                "speaking",
                "bol raha hoon",
                "bol rahi hoon",
                "mi boltoy",
                "mi boltey",
                "mi boltoy ho",
                "mi boltey ho",
                "मी बोलतोय",
                "मी बोलतेय",
                "हो मी",
            ],
            "wrong_person": [
                "wrong number",
                "not this person",
                "you have wrong person",
                "गलत नंबर",
                "गलत व्यक्ति",
                "चुकीचा नंबर",
                "मी नाही",
            ],
            "busy_now": [
                "busy",
                "not now",
                "call later",
                "later",
                "atta nahi",
                "ata nahi",
                "nantar bola",
                "nantar call kara",
                "बाद में",
                "अभी नहीं",
                "नंतर",
                "आत्ता नाही",
            ],
            "ask_call_back_time": [
                "when will you call",
                "what time will you call",
                "callback time",
                "kiti vajta call",
                "kiti veles call",
                "kadhi call",
                "कब कॉल करोगे",
                "कॉलबॅक कधी",
                "कॉल कधी",
            ],
            "reschedule": [
                "reschedule",
                "change time",
                "change day",
                "another day",
                "udya kara",
                "parwa kara",
                "vel badla",
                "divas badla",
                "दूसरा समय",
                "दुसरी वेळ",
                "पुन्हा वेळ",
            ],
            "ask_price": [
                "price",
                "cost",
                "charges",
                "fees",
                "kimmat",
                "kitna padega",
                "kiti paise",
                "कितना",
                "किंमत",
                "दर",
            ],
            "ask_location": [
                "where is",
                "location",
                "address",
                "कहाँ",
                "पत्ता",
                "कुठे",
            ],
            "ask_doctor_availability": [
                "doctor available",
                "doctor availability",
                "which doctor",
                "doctors available",
                "डॉक्टर उपलब्ध",
                "कौन डॉक्टर",
                "कोण डॉक्टर",
            ],
            "not_interested": [
                "not interested",
                "no interest",
                "don't need",
                "do not need",
                "interest nahi",
                "nakko",
                "nako aahe",
                "नहीं चाहिए",
                "रुचि नहीं",
                "इंटरेस्ट नहीं",
                "नको",
            ],
            "already_booked": [
                "already booked",
                "booked already",
                "already done",
                "पहले से बुक",
                "already appointment",
                "आधीच बुक",
            ],
            "human_agent_request": [
                "human",
                "real person",
                "connect agent",
                "call from staff",
                "टीम से बात",
                "मानव से बात",
                "प्रत्यक्ष व्यक्ती",
                "प्रतिनिधीशी बोला",
            ],
            "language_switch": [
                "speak hindi",
                "speak english",
                "marathi bol",
                "हिंदी में बोलो",
                "english mein",
                "मराठीत बोला",
            ],
            "unclear_audio": [
                "not clear",
                "cannot hear",
                "can't hear",
                "voice breaking",
                "आवाज नहीं आ रहा",
                "आवाज कट रही",
                "आवाज तुटतोय",
                "स्पष्ट नाही",
            ],
            "repeat_request": [
                "repeat",
                "say again",
                "again",
                "punha sanga",
                "parat sanga",
                "समझा नहीं",
                "पुन्हा सांगा",
                "दोबारा बोलो",
            ],
            "consent_yes": [
                "yes you can continue",
                "go ahead",
                "continue",
                "ho bola",
                "haan bolo",
                "हाँ बोलो",
                "हो बोला",
            ],
            "consent_no": [
                "do not continue",
                "stop call",
                "not now",
                "nako bola",
                "ata nako",
                "मत बोलो",
                "नको पुढे",
            ],
            "do_not_call": [
                "do not call",
                "dont call",
                "never call",
                "number remove",
                "कॉल मत करना",
                "पुन्हा कॉल करू नका",
                "डू नॉट कॉल",
            ],
        }

    def _merged_intent_phrase_catalog(self) -> dict[str, list[str]]:
        merged: dict[str, list[str]] = {}
        base_catalog = self._base_intent_phrase_catalog()
        for intent_name, phrases in base_catalog.items():
            merged[intent_name] = list(dict.fromkeys(phrases))

        def _merge_overrides(overrides: dict[str, list[str]] | None) -> None:
            if not isinstance(overrides, dict):
                return
            for intent_name, phrases in overrides.items():
                if not intent_name:
                    continue
                normalized_intent = str(intent_name).strip().lower()
                if not normalized_intent:
                    continue
                if isinstance(phrases, list):
                    normalized_phrases = [str(item).strip() for item in phrases if str(item).strip()]
                else:
                    normalized_phrases = [
                        part.strip()
                        for part in str(phrases).split(",")
                        if isinstance(part, str) and part.strip()
                    ]
                if not normalized_phrases:
                    continue
                existing = merged.get(normalized_intent, [])
                merged[normalized_intent] = list(dict.fromkeys([*existing, *normalized_phrases]))

        _merge_overrides(self.client.config.conversation.intent_overrides)
        if self.project is not None:
            _merge_overrides(self.project.intent_overrides)
        return merged

    @staticmethod
    def _matches_intent_phrase(raw_text: str, normalized_text: str, padded_text: str, phrase: str) -> bool:
        candidate = str(phrase).strip()
        if not candidate:
            return False
        candidate_lower = candidate.lower()
        raw_lower = raw_text.lower()
        if any(ord(char) > 127 for char in candidate):
            return candidate in raw_text or candidate_lower in raw_lower
        normalized_candidate = VoiceSalesSession._normalize_intent_text(candidate)
        if not normalized_candidate:
            return False
        if normalized_text == normalized_candidate:
            return True
        if " " in normalized_candidate:
            return normalized_candidate in normalized_text
        return f" {normalized_candidate} " in padded_text

    @staticmethod
    def _normalize_intent_text(text: str) -> str:
        normalized = text.lower().strip(" .,!?:;")
        replacements = {
            "udhya": "udya",
            "udyaa": "udya",
            "parva": "parwa",
            "parवा": "parwa",
            "parvaa": "parwa",
            "parvya": "parwa",
            "sakali": "sakal",
            "sakaali": "sakal",
            "sayankali": "sayankal",
            "sandhyakali": "sandhyakal",
            "ho na": "hona",
            "haan na": "haanna",
        }
        for source, target in replacements.items():
            normalized = normalized.replace(source, target)
        return " ".join(normalized.split())

    @staticmethod
    def _contains_any(text: str, phrases: set[str]) -> bool:
        return any(phrase in text for phrase in phrases)

    async def _stop_after_playback(self) -> None:
        with contextlib.suppress(Exception):
            await self.audio.wait_for_playback_idle()
        await asyncio.sleep(AUTO_STOP_GRACE_SECONDS)
        self.stop()

    def _set_status(self, status: str, detail: str) -> None:
        key = (status, detail)
        if self._status_key == key:
            return
        self._status_key = key
        self._sync_processing_ambience(status)
        self.console.print(f"[yellow][status][/yellow] {status.upper()} | {detail}")
        asyncio.create_task(self._emit("status", status=status, detail=detail))

    def _sync_processing_ambience(self, status: str) -> None:
        if not self.settings.processing_ambience_enabled:
            enabled = False
        else:
            enabled = status == "processing" and self._is_exotel_session()
        if hasattr(self.audio, "set_processing_ambience"):
            with contextlib.suppress(RuntimeError):
                asyncio.create_task(self.audio.set_processing_ambience(enabled))

    def _is_closing_turn(self, speaker: str, text: str) -> bool:
        lowered = text.lower().strip()
        thanks_markers = [
            "thank you",
            "thanks",
            "धन्यवाद",
            "शुक्रिया",
            "thankyou",
            "thank you so much",
            "thanks for your time",
        ]
        closing_markers = [
            "next step",
            "demo",
            "calendar invite",
            "follow up",
            "bye",
            "goodbye",
            "talk to you",
            "पुढची पायरी",
            "डेमो",
            "भेटू",
            "पुन्हा बोलू",
            "कॉल ठेवतो",
            "नमस्कार",
            "follow-up",
            "मी invite पाठवतो",
            "मी इन्व्हाईट पाठवतो",
            "वेळ दिल्याबद्दल",
            "appointment confirmed",
            "your appointment is confirmed",
            "your enquiry has been received",
            "enquiry has been received",
            "अपॉइंटमेंट कन्फर्म",
            "appointment confirm",
            "आपकी appointment",
            "आपकी enquiry",
            "तुमची appointment",
            "तुमची enquiry",
        ]
        has_thanks = any(marker in lowered for marker in thanks_markers)
        has_closing = any(marker in lowered for marker in closing_markers)
        is_question = VoiceSalesSession._looks_like_question(text)
        if is_question:
            return False
        if speaker == "agent":
            if self.client.config.closure_mode == "appointment_booking":
                return self._is_appointment_booking_closure(text)
            return has_closing or has_thanks
        word_count = len(lowered.split())
        return has_thanks or (has_closing and word_count <= 4)

    def _should_auto_stop_after_agent_turn(self, text: str) -> bool:
        lowered = text.lower().strip()
        if not lowered:
            return False
        if self._looks_like_question(text):
            return False
        if self.client.config.closure_mode == "appointment_booking":
            return self._is_appointment_booking_closure(text)
        ganpati_markers = [
            "appointment confirmed",
            "your appointment is confirmed",
            "your enquiry has been received",
            "enquiry has been received",
            "अपॉइंटमेंट कन्फर्म",
            "appointment confirm",
            "आपकी appointment",
            "आपकी enquiry",
            "तुमची appointment",
            "तुमची enquiry",
        ]
        if any(marker in lowered for marker in ganpati_markers):
            return True
        if not self._looks_like_scheduling_commitment(self._last_committed_user_text):
            return False
        scheduling_ack_markers = [
            "perfect",
            "great",
            "okay",
            "ok",
            "done",
            "sure",
            "confirmed",
            "invite",
            "calendar",
            "follow",
            "पुढच्या",
            "पुढची",
            "ठीक",
            "छान",
            "नक्की",
            "इन्व्हाईट",
            "भेटू",
            "धन्यवाद",
            "thanks",
        ]
        return any(marker in lowered for marker in scheduling_ack_markers)

    def _is_appointment_booking_closure(self, text: str) -> bool:
        lowered = text.lower().strip()
        if not lowered or VoiceSalesSession._looks_like_question(text):
            return False
        configured_non_closing = self.client.config.closure_examples.non_closing_questions
        if self._matches_closure_example(text, configured_non_closing):
            return False
        configured_positive = self.client.config.closure_examples.positive
        configured_negative = self.client.config.closure_examples.negative
        if self._matches_closure_example(text, configured_positive):
            return True
        if self._matches_closure_example(text, configured_negative):
            return True
        strong_closure_markers = [
            "appointment confirmed",
            "your appointment is confirmed",
            "appointment has been confirmed",
            "your enquiry has been received",
            "enquiry has been received",
            "your inquiry has been received",
            "inquiry has been received",
            "appointment booked",
            "appointment has been booked",
            "अपॉइंटमेंट",
            "कन्फर्म झाली",
            "कन्फर्म हो गई",
            "बुक हो गई",
            "बुकिंग हो गई",
            "निश्चित झाली",
            "निश्चित झाले",
            "चौकशी नोंदवली",
            "चौकशी प्राप्त झाली",
        ]
        has_confirmation = any(marker in lowered for marker in strong_closure_markers)
        non_closing_markers = [
            "कोणता दिवस",
            "कौन सा दिन",
            "what day",
            "what time",
            "कौन सा समय",
            "वेळ हवी आहे",
            "वेळ पाहिजे",
            "time would you prefer",
            "when to book",
            "appointment हवी आहे",
            "appointment चाहिए",
            "अपॉइंटमेंट चाहिए",
        ]
        if any(marker in lowered for marker in non_closing_markers):
            return False
        negative_closure_markers = [
            "don't want appointment",
            "do not want appointment",
            "no appointment",
            "not interested in booking",
            "i do not need an appointment",
            "appointment नहीं चाहिए",
            "अपॉइंटमेंट नहीं चाहिए",
            "बुकिंग नहीं चाहिए",
            "मुझे अपॉइंटमेंट नहीं चाहिए",
            "अपॉइंटमेंट नको",
            "बुकिंग नको",
            "ठीक है धन्यवाद",
            "ठीक आहे धन्यवाद",
            "okay thank you",
        ]
        finality_markers = [
            "thank you",
            "thanks",
            "धन्यवाद",
            "confirmed",
            "कन्फर्म",
            "निश्चित",
            "received",
            "नोंदवली",
            "recorded",
            "booked",
            "बुक",
            "नहीं चाहिए",
            "नको",
        ]
        if any(marker in lowered for marker in negative_closure_markers):
            return any(marker in lowered for marker in finality_markers)
        if not has_confirmation:
            return False
        return any(marker in lowered for marker in finality_markers)

    @staticmethod
    def _matches_closure_example(text: str, examples: list[str]) -> bool:
        normalized_text = VoiceSalesSession._normalize_match_text(text)
        if not normalized_text:
            return False
        for example in examples:
            normalized_example = VoiceSalesSession._normalize_match_text(example)
            if not normalized_example:
                continue
            if normalized_example in normalized_text or normalized_text in normalized_example:
                return True
        return False

    @staticmethod
    def _normalize_match_text(text: str) -> str:
        normalized = " ".join(text.lower().split()).strip()
        normalized = normalized.strip(" .,!?:;।")
        return normalized

    @staticmethod
    def _looks_like_question(text: str) -> bool:
        lowered = text.lower().strip()
        if "?" in text:
            return True
        question_markers = [
            " right",
            "right?",
            " kya ",
            " क्या ",
            " ka ",
            " का ",
        ]
        padded = f" {lowered} "
        return any(marker in padded for marker in question_markers)

    def _mark_binary_confirmation_state(self, text: str) -> None:
        self._update_scripted_stage_from_agent_text(text)
        if self._looks_like_binary_confirmation_question(text):
            self._awaiting_binary_confirmation = True
            self._binary_confirmation_prompted_at = monotonic()
            self._binary_confirmation_resolved = False
            return
        self._awaiting_binary_confirmation = False
        self._binary_confirmation_resolved = True

    def _update_scripted_stage_from_agent_text(self, text: str) -> None:
        if not self._is_fast_turn_mode():
            return
        if self._scripted_flow_stage == "closing_confirmation":
            return
        lowered = text.lower().strip()
        if not lowered:
            return
        current = self._scripted_flow_stage
        if current in {"collect_family_history", "schedule_preference", "time_window", "callback_preference"} and any(
            marker in lowered for marker in {"कोणता दिवस", "कौन सा दिन", "tomorrow or the day after", "उद्या की परवा", "कल आना"}
        ):
            self._scripted_flow_stage = "schedule_preference"
            return
        if current in {"schedule_preference", "time_window"} and any(
            marker in lowered for marker in {"सकाळ", "दुपार", "सायंकाळ", "सुबह", "दोपहर", "शाम", "morning", "afternoon", "evening"}
        ):
            self._scripted_flow_stage = "time_window"
            return
        if current in {"schedule_preference", "time_window", "callback_preference"} and any(
            marker in lowered for marker in {"कॉलबॅक", "callback", "बाद में", "पुन्हा कॉल"}
        ):
            self._scripted_flow_stage = "callback_preference"
            return
        if any(marker in lowered for marker in {"तुमचं नाव", "क्या मैं", "am i speaking with", "कोणाशी बोलत", "किससे बात"}):
            self._scripted_flow_stage = "identity_confirmation"
            return
        # Avoid resetting the stage backwards once scheduling/callback has begun.
        if current in {"none", "identity_confirmation", "opening_confirmation"} and any(
            marker in lowered for marker in {"हार्ट चेकअप", "अपॉइंटमेंट", "appointment", "बुक"}
        ):
            self._scripted_flow_stage = "collect_age"

    def _looks_like_binary_confirmation_question(self, text: str) -> bool:
        lowered = text.lower().strip()
        if not self._looks_like_question(text):
            return False
        if self._looks_like_scheduling_commitment(text):
            return False
        binary_markers = [
            " आहे का",
            " आहात का",
            "हवी आहे का",
            "करून घ्यायची आहे का",
            "का?",
            "क्या आप",
            "क्या आपका",
            "क्या मैं",
            "would you like",
            "am i speaking with",
            "are you",
            "do you want",
        ]
        return any(marker in lowered or marker in text for marker in binary_markers)

    @staticmethod
    def _looks_like_scheduling_commitment(text: str) -> bool:
        lowered = text.lower().strip()
        if not lowered:
            return False
        time_pattern = re.compile(
            r"\b\d{1,2}(:\d{2})?\s*(am|pm)?\b|"
            r"\b\d{1,2}\s*वाजता\b|"
            r"\b\d{1,2}\s*बजे\b"
        )
        if time_pattern.search(lowered):
            return True
        day_markers = [
            "tomorrow",
            "today",
            "morning",
            "evening",
            "उद्या",
            "आज",
            "सकाळी",
            "सायंकाळी",
            "कल",
            "आज",
            "सुबह",
            "शाम",
        ]
        return any(marker in lowered for marker in day_markers)

    def _print_banner(self) -> None:
        table = Table(title="Local Gemini Voice Sales Demo", show_header=False)
        table.add_row("Client", self.client.config.display_name)
        table.add_row("Industry", self.client.config.industry)
        table.add_row("Offer", self.client.config.primary_offer)
        table.add_row("Voice", self.client.config.voice.voice_name)
        table.add_row("Session Output", str(self.session_dir))
        table.add_row("Future Telephony", "TODO hooks are isolated in the conversation and audio layers.")
        self.console.print(table)

    def _print_summary_panel(self) -> None:
        self.console.print(f"\nSession files saved to [bold]{self.session_dir}[/bold]")
        if self.artifacts.summary:
            self.console.print(f"[bold]Summary:[/bold] {self.artifacts.summary.summary}")
            self.console.print(
                f"[bold]Suggested next action:[/bold] {self.artifacts.summary.suggested_next_action}"
            )

    async def _deliver_native_agent_line(self, text: str) -> None:
        self._set_status("speaking", "Starting native audio reply...")
        self._mark_binary_confirmation_state(text)
        prompt_requested_at = time.time()
        self.artifacts.metrics["initial_prompt_requested_at_epoch"] = prompt_requested_at
        if isinstance(self.telephony_context, dict):
            self.telephony_context["initial_prompt_requested_at_epoch"] = prompt_requested_at
        if hasattr(self.audio, "_send_mark"):
            try:
                mark_name = "opening_started" if text == self._render_opening_text() else "native_reply_started"
                await self.audio._send_mark(mark_name)
            except Exception:
                logger.debug("Failed to emit Exotel opening mark.", exc_info=True)
        self._agent_audio_deadline = monotonic() + NATIVE_PROMPT_PREROLL_SECONDS
        self._native_line_in_flight = True
        self._native_line_completed.clear()
        self._waiting_for_user_after_agent = False
        prompt = (
            "Speak exactly the following line and nothing else. "
            f"Do not add any introduction or explanation: {text}"
        )
        await self.live.send_text_turn(prompt, role="user", turn_complete=True)
        self._pending_native_line = text
        self.session_logger.append_turn(
            self.artifacts,
            TranscriptTurn(speaker="agent", text=text, turn_id=self._turn_index),
        )
        if text == self._render_opening_text():
            self._opening_delivered = True
        self.console.print(f"[bold green]Agent:[/bold green] {text}")
        await self._emit("turn", speaker="agent", text=text, turn_id=self._turn_index)

    def _record_telephony_timing_metrics(self) -> None:
        if not isinstance(self.telephony_context, dict):
            return

        def numeric(name: str) -> float | None:
            value = self.telephony_context.get(name)
            if isinstance(value, (int, float)):
                return float(value)
            return None

        pairs = [
            ("call_requested_at_epoch", "exotel_ws_url_requested_at_epoch", "exotel_call_to_ws_url_ms"),
            ("exotel_ws_url_requested_at_epoch", "exotel_connected_at_epoch", "exotel_ws_url_to_connected_ms"),
            ("exotel_connected_at_epoch", "exotel_start_at_epoch", "exotel_connected_to_start_ms"),
            ("exotel_start_at_epoch", "initial_prompt_requested_at_epoch", "exotel_start_to_prompt_request_ms"),
        ]
        for start_key, end_key, metric_key in pairs:
            start = numeric(start_key)
            end = numeric(end_key)
            if start is None or end is None or end < start:
                continue
            self.artifacts.metrics[metric_key] = round((end - start) * 1000, 3)

    def _should_flush_for_barge_in(self) -> bool:
        now = monotonic()
        if (now - self._last_user_transcription_at) > self._interruption_grace_seconds():
            return False
        if (now - self._last_playback_flush_at) < self._barge_in_debounce_seconds():
            return False
        snippet = self._latest_user_snippet.strip()
        if len(snippet) < MIN_BARGE_IN_CHARACTERS and not self._latest_user_snippet_final:
            return False
        return bool(snippet)

    def _normalize_agent_text(self, agent_text: str) -> str:
        cleaned = " ".join(agent_text.replace("**", " ").split()).strip()
        if not cleaned:
            return cleaned

        banned_fragments = [
            "initiating the sales call",
            "adapting the opening",
            "i'm now ready to start",
            "i will avoid",
            "i will greet",
            "preferred opening style",
            "after the prospect replies",
            "internal notes",
            "meta commentary",
            "my focus now",
            "my goal is",
            "i'm aiming to",
            "i plan to",
            "i've interpreted",
            "as per the playbook",
            "keeping with the playbook",
            "confirming user intent",
            "acknowledge and initiate",
            "refocusing",
            "clarifying understanding",
            "clarify the user's",
            "provide clear details",
            "confirm specific available dates and times",
            "negative comment",
        ]
        lowered = cleaned.lower()
        if not any(fragment in lowered for fragment in banned_fragments) and "**" not in agent_text:
            return cleaned

        sentence_candidates = [
            part.strip(" -*")
            for part in re.split(r"(?<=[.!?।])\s+|\n+", cleaned)
            if part.strip(" -*")
        ]
        kept: list[str] = []
        for sentence in sentence_candidates:
            lowered_sentence = sentence.lower()
            if any(fragment in lowered_sentence for fragment in banned_fragments):
                continue
            if re.search(r"[A-Za-z]", sentence) and not re.search(r"[\u0900-\u097F]", sentence):
                if any(
                    marker in lowered_sentence
                    for marker in ("i ", "i'm", "my ", "we will", "i will", "i plan", "focus", "goal")
                ):
                    continue
            kept.append(sentence)

        if kept:
            cleaned = " ".join(kept).strip()

        cleaned = re.sub(r"^[A-Za-z][A-Za-z '&/-]{2,}:\s*", "", cleaned).strip()
        cleaned = cleaned.strip(" -*")
        token_count = len(cleaned.split())
        if token_count <= 2 and len(cleaned) <= 12 and not re.search(r"[?.!।]", cleaned):
            return ""
        if re.fullmatch(r"[\u0900-\u097F]{1,3}", cleaned):
            return ""
        return self._enforce_appointment_stage_question(cleaned)

    def _render_age_question(self) -> str:
        if self.current_language == "marathi":
            return "धन्यवाद. तुमचं वय किती आहे?"
        if self.current_language == "hindi":
            return "धन्यवाद। आपकी उम्र कितनी है?"
        return "Thank you. What is the age?"

    def _render_chest_pain_question(self) -> str:
        if self.current_language == "marathi":
            return "सध्या छातीत दुखणे, जडपणा किंवा दाब जाणवतो का? हो की नाही?"
        if self.current_language == "hindi":
            return "अभी सीने में दर्द, भारीपन या दबाव है क्या? हाँ या नहीं?"
        return "Do you currently have chest pain, heaviness, or pressure? Yes or no?"

    def _render_family_history_question(self) -> str:
        if self.current_language == "marathi":
            return "कुटुंबात आधी हृदयविकाराचा इतिहास आहे का? हो की नाही?"
        if self.current_language == "hindi":
            return "परिवार में पहले से हार्ट डिजीज का इतिहास है क्या? हाँ या नहीं?"
        return "Is there any previous history of heart disease in the family? Yes or no?"

    @staticmethod
    def _classify_agent_question_step(text: str) -> str:
        lowered = text.lower()
        if any(marker in lowered for marker in ("am i speaking with", "क्या मैं", "किससे बात", "कोणाशी बोलत", "बोलते आहे का")):
            return "identity"
        if any(marker in lowered for marker in ("age", "उम्र", "वय")):
            return "age"
        if any(
            marker in lowered
            for marker in ("chest pain", "heaviness", "pressure", "सीने", "छातीत", "दर्द", "दुखणे", "जडपणा")
        ):
            return "chest"
        if any(
            marker in lowered
            for marker in ("family history", "परिवार", "कुटुंबात", "heart disease", "हार्ट डिजीज", "हृदयविकार")
        ):
            return "family"
        if any(
            marker in lowered
            for marker in ("tomorrow", "day after", "कल", "परसों", "उद्या", "परवा", "कोणता दिवस", "कौन सा दिन")
        ):
            return "schedule"
        return "other"

    def _enforce_appointment_stage_question(self, text: str) -> str:
        if self.client.config.conversation_mode != "appointment_booking":
            return text
        if self._scripted_flow_stage == "closing_confirmation":
            return text

        step = self._classify_agent_question_step(text)
        if self._scripted_flow_stage == "identity_confirmation":
            # Only advance after the caller has spoken at least once.
            if self._has_user_spoken and step in {"schedule", "chest", "family"}:
                self._scripted_flow_stage = "collect_age"
                return self._render_age_question()
            return text
        if self._scripted_flow_stage == "collect_age" and step in {"schedule", "family", "chest"}:
            return self._render_age_question()
        if self._scripted_flow_stage == "collect_chest_pain" and step in {"schedule", "family"}:
            return self._render_chest_pain_question()
        if self._scripted_flow_stage == "collect_family_history" and step == "schedule":
            return self._render_family_history_question()
        return text

    @staticmethod
    def _is_short_response_candidate(text: str) -> bool:
        normalized = VoiceSalesSession._normalize_intent_text(text)
        if not normalized:
            return False
        if len(normalized.split()) > 2:
            return False
        short_replies = {
            "yes",
            "yeah",
            "yep",
            "no",
            "nope",
            "ok",
            "okay",
            "haan",
            "han",
            "ha",
            "haa",
            "ji",
            "jee",
            "ho",
            "hoy",
            "ho na",
            "hm",
            "hmm",
            "nahi",
            "nahi",
            "nahin",
            "na",
            "नाही",
            "नको",
            "हो",
            "होय",
            "हो ना",
            "हं",
            "हाँ",
            "हां",
            "हा",
            "जी",
            "ना",
            "ओके",
        }
        return normalized in short_replies

    def _render_opening_text(self) -> str:
        display_name = self.client.config.display_name
        configured_opening = self.client.config.opening_script.get(self.opening_language)
        if configured_opening:
            return configured_opening.format(
                hospital_name=display_name,
                project_name=self.project.name if self.project else display_name,
                customer_name=self.customer_name,
            )

        if self._is_exotel_session():
            if self.opening_language == "marathi":
                return f"नमस्कार, मी {self.customer_name} यांच्याशी बोलते आहे का?"
            if self.opening_language == "hindi":
                return f"नमस्कार, क्या मैं {self.customer_name} से बात कर रही हूँ?"
            return f"Hello, am I speaking with {self.customer_name}?"
        project_id = str(self.project.project_id or "").strip().lower() if self.project else ""
        if self._is_guest_demo_workspace():
            if project_id == "real_estate_english_demo":
                return (
                    f"Hello {self.customer_name}, welcome to our Real Estate Demo. "
                    "How may I help you with your property search today?"
                )
            if project_id == "magnum_hospital_marathi_demo":
                return (
                    f"नमस्कार {self.customer_name}, Magnum Hospital डेमोमध्ये तुमचं स्वागत आहे. "
                    "तुम्हाला अपॉइंटमेंट बुक करायची आहे का, की कोणत्या विभागासाठी चौकशी करायची आहे?"
                )
            if project_id == "janardan_swami_cancer_helpdesk_demo":
                return (
                    f"नमस्कार {self.customer_name}, Cancer Helpdesk Demo मध्ये तुमचं स्वागत आहे. "
                    "कृपया सांगा, तुम्हाला कॅन्सर उपचाराबद्दल कोणती माहिती हवी आहे?"
                )
            if project_id == "car_dealer_hindi_demo":
                return (
                    f"नमस्कार {self.customer_name}, Car Dealer Demo में आपका स्वागत है। "
                    "क्या आप नई कार देख रहे हैं, टेस्ट ड्राइव चाहते हैं, या एक्सचेंज के बारे में पूछना चाहते हैं?"
                )
            if project_id == "aivoicebot4u_english_demo":
                return (
                    f"Hello {self.customer_name}, welcome to the AI Voice Bot 4 U Demo. "
                    "For your requirement, are you looking more for inbound call handling, outbound calling, or appointment booking?"
                )
        if self.client.config.conversation_mode == "appointment_booking":
            if self.opening_language == "marathi":
                return f"नमस्कार, मी {display_name} मधून बोलते आहे. तुमचं नाव {self.customer_name} आहे का?"
            if self.opening_language == "hindi":
                return f"नमस्कार, मैं {display_name} से बोल रही हूँ। क्या आपका नाम {self.customer_name} है?"
            return f"Hello, this is {display_name}. Am I speaking with {self.customer_name}?"
        persona_gender = self.client.config.voice.persona_gender
        if self.opening_language == "marathi":
            verb = "बोलते आहे" if persona_gender == "female" else "बोलत आहे"
            return f"नमस्कार, मी {display_name} कडून {verb}. तुमचं नाव {self.customer_name} आहे का?"
        if self.opening_language == "hindi":
            verb = "बोल रही हूँ" if persona_gender == "female" else "बोल रहा हूँ"
            return f"नमस्कार, मैं {display_name} से {verb}। क्या आपका नाम {self.customer_name} है?"
        return f"Hello, this is {display_name}. Am I speaking with {self.customer_name}?"

    def _render_silence_follow_up_text(self) -> str:
        if self.current_language == "marathi":
            return "हॅलो, तुम्ही आहात का? काही प्रश्न आहेत का?"
        if self.current_language == "hindi":
            return "हैलो, क्या आप वहाँ हैं? क्या आपके कोई सवाल हैं?"
        return "Hello, are you there? Do you have any questions?"

    def _should_send_guest_silence_followup(self) -> bool:
        if not self._running or not self._is_guest_demo_workspace():
            return False
        if not self._opening_delivered or not self._waiting_for_user_after_agent:
            return False
        if self._silence_follow_up_sent:
            return False
        if self.audio.is_playing():
            return False
        if self._pending_native_line is not None or self._native_line_in_flight:
            return False
        if self._partial_turns.get("user") or self._turn_buffers.get("user"):
            return False
        if self._last_agent_turn_at <= 0.0:
            return False
        return (monotonic() - self._last_agent_turn_at) >= GUEST_SILENCE_FOLLOW_UP_SECONDS

    def _render_low_confidence_reprompt(self) -> str:
        if self.current_language == "marathi":
            return "माफ करा, आवाज नीट आला नाही. कृपया हो किंवा नाही सांगा."
        if self.current_language == "hindi":
            return "माफ कीजिए, आवाज साफ़ नहीं आया। कृपया हाँ या नहीं कहिए।"
        return "Sorry, I did not catch that clearly. Please say yes or no."

    def _render_live_reconnect_line(self) -> str:
        if self.current_language == "marathi":
            return "एक क्षण, कनेक्शन पुन्हा जोडले आहे. आपण पुढे बोलूया."
        if self.current_language == "hindi":
            return "एक क्षण, कनेक्शन फिर से जुड़ गया है। हम आगे बात जारी रखते हैं।"
        return "One moment, the connection is back. Let's continue."

    async def _emit(self, kind: str, **payload: object) -> None:
        if self.event_handler is None:
            return
        event = SessionEvent(kind=kind, payload=dict(payload))
        result = self.event_handler(event)
        if asyncio.iscoroutine(result):
            await result

    @staticmethod
    def _is_clean_disconnect(exc: Exception) -> bool:
        message = str(exc)
        return (
            isinstance(exc, ConnectionClosed)
            and getattr(exc, "code", None) == 1000
            or isinstance(exc, APIError)
            and getattr(exc, "code", None) == 1000
            or isinstance(exc, APIError)
            and getattr(exc, "status_code", None) == 1000
            or isinstance(exc, APIError)
            and getattr(exc, "status_code", None) == 1011
            and "Deadline expired" in str(exc)
            or "keepalive ping timeout" in message
            or "abnormal closure" in message
            or "timed out while closing connection" in message
        )

    @staticmethod
    def _is_retryable_live_error(exc: Exception) -> bool:
        message = str(exc or "")
        if isinstance(exc, APIError):
            status_code = getattr(exc, "status_code", None)
            code = getattr(exc, "code", None)
            if status_code == 1011 or code == 1011:
                return True
        if isinstance(exc, ConnectionClosed) and getattr(exc, "code", None) == 1011:
            return True
        return "Internal error encountered" in message

    @staticmethod
    def _is_retryable_structured_error(exc: Exception) -> bool:
        message = str(exc or "").upper()
        markers = ("429", "500", "502", "503", "504", "RESOURCE_EXHAUSTED", "UNAVAILABLE", "DEADLINE_EXCEEDED")
        return any(marker in message for marker in markers)

    @staticmethod
    def _describe_exception(exc: Exception) -> str:
        if isinstance(exc, ConnectionClosed) and getattr(exc, "code", None) == 1000:
            return "Gemini Live session ended normally."
        if isinstance(exc, APIError) and getattr(exc, "status_code", None) == 1011 and "Deadline expired" in str(exc):
            return (
                "Gemini Live timed out while waiting on the current turn. "
                "This usually happens after fragmented or very short speech during the native-audio session. "
                "Start a fresh session and continue with slightly longer replies."
            )
        if "keepalive ping timeout" in str(exc) or "abnormal closure" in str(exc) or "timed out while closing connection" in str(exc):
            return "Gemini Live connection dropped during shutdown. You can start a fresh session."

        message = str(exc).strip()
        if "Invalid input device" in message or "No Default Input Device Available" in message:
            return "Microphone access failed. Check your OS microphone permissions and active input device."
        if "Invalid output device" in message or "No Default Output Device Available" in message:
            return "Speaker output failed. Check your active audio output device."
        return message or exc.__class__.__name__
