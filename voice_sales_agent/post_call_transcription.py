"""Event-driven post-call transcription and summary pipeline."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import sqlite3
import tempfile
import wave
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any
from uuid import uuid4

from google import genai
from google.genai import types

from .config import AppSettings
from .extraction import build_key_details_prompt, clear_non_caller_location, parse_key_details_payload
from .faster_stt import FasterWhisperTranscriber
from .models import RecordingKeyDetails

logger = logging.getLogger(__name__)


def _read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


@dataclass(slots=True)
class RecordingTranscriptionJob:
    account_id: str
    session_id: str
    caller_number: str | None
    recording_path: str
    recording_status: str = "saved"
    language_hint: str | None = None
    notify_whatsapp_number: str | None = None


class SqliteCallTranscriptionStore:
    """Persist transcription and summary rows for saved call recordings."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS call_transcripts (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    caller_number TEXT,
                    recording_path TEXT NOT NULL,
                    transcript_text TEXT NOT NULL,
                    language TEXT,
                    transcription_provider TEXT NOT NULL,
                    transcription_model TEXT NOT NULL,
                    confidence REAL,
                    transcription_status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(session_id, account_id)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_call_transcripts_account_status
                ON call_transcripts(account_id, transcription_status, created_at DESC)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS call_summaries (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    keywords TEXT NOT NULL,
                    sentiment TEXT NOT NULL,
                    action_items TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(session_id, account_id)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_call_summaries_account_created
                ON call_summaries(account_id, created_at DESC)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS call_transcript_turns (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    turn_index INTEGER NOT NULL,
                    speaker_label TEXT,
                    speaker_role TEXT NOT NULL,
                    text TEXT NOT NULL,
                    timestamp_ms REAL,
                    start_offset_ms REAL,
                    end_offset_ms REAL,
                    confidence REAL,
                    source_name TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(session_id, account_id, turn_index)
                )
                """
            )
            existing_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(call_transcript_turns)").fetchall()
            }
            if "timestamp_ms" not in existing_columns and existing_columns:
                with contextlib.suppress(Exception):
                    conn.execute("ALTER TABLE call_transcript_turns ADD COLUMN timestamp_ms REAL")
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_call_transcript_turns_account_created
                ON call_transcript_turns(account_id, created_at DESC)
                """
            )
            conn.commit()

    def upsert_transcript(
        self,
        *,
        session_id: str,
        account_id: str,
        caller_number: str | None,
        recording_path: str,
        transcript_text: str,
        language: str | None,
        transcription_provider: str,
        transcription_model: str,
        confidence: float | None,
        transcription_status: str,
        started_at: str,
        completed_at: str | None,
    ) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        row = {
            "id": str(uuid4()),
            "session_id": session_id,
            "account_id": account_id,
            "caller_number": caller_number,
            "recording_path": recording_path,
            "transcript_text": transcript_text,
            "language": language,
            "transcription_provider": transcription_provider,
            "transcription_model": transcription_model,
            "confidence": confidence,
            "transcription_status": transcription_status,
            "started_at": started_at,
            "completed_at": completed_at,
            "created_at": now_iso,
            "updated_at": now_iso,
        }
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id, created_at FROM call_transcripts WHERE session_id = ? AND account_id = ? LIMIT 1",
                (session_id, account_id),
            ).fetchone()
            if existing is not None:
                row["id"] = str(existing["id"])
                row["created_at"] = str(existing["created_at"])
            conn.execute(
                """
                INSERT INTO call_transcripts(
                    id, session_id, account_id, caller_number, recording_path, transcript_text,
                    language, transcription_provider, transcription_model, confidence,
                    transcription_status, started_at, completed_at, created_at, updated_at
                )
                VALUES(
                    :id, :session_id, :account_id, :caller_number, :recording_path, :transcript_text,
                    :language, :transcription_provider, :transcription_model, :confidence,
                    :transcription_status, :started_at, :completed_at, :created_at, :updated_at
                )
                ON CONFLICT(session_id, account_id) DO UPDATE SET
                    caller_number = excluded.caller_number,
                    recording_path = excluded.recording_path,
                    transcript_text = excluded.transcript_text,
                    language = excluded.language,
                    transcription_provider = excluded.transcription_provider,
                    transcription_model = excluded.transcription_model,
                    confidence = excluded.confidence,
                    transcription_status = excluded.transcription_status,
                    started_at = excluded.started_at,
                    completed_at = excluded.completed_at,
                    updated_at = excluded.updated_at
                """,
                row,
            )
            conn.commit()

    def upsert_summary(
        self,
        *,
        session_id: str,
        account_id: str,
        summary: str,
        keywords: list[str],
        sentiment: str,
        action_items: list[str],
    ) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        row = {
            "id": str(uuid4()),
            "session_id": session_id,
            "account_id": account_id,
            "summary": summary,
            "keywords": json.dumps(keywords, ensure_ascii=False),
            "sentiment": sentiment,
            "action_items": json.dumps(action_items, ensure_ascii=False),
            "created_at": now_iso,
        }
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id, created_at FROM call_summaries WHERE session_id = ? AND account_id = ? LIMIT 1",
                (session_id, account_id),
            ).fetchone()
            if existing is not None:
                row["id"] = str(existing["id"])
                row["created_at"] = str(existing["created_at"])
            conn.execute(
                """
                INSERT INTO call_summaries(
                    id, session_id, account_id, summary, keywords, sentiment, action_items, created_at
                )
                VALUES(
                    :id, :session_id, :account_id, :summary, :keywords, :sentiment, :action_items, :created_at
                )
                ON CONFLICT(session_id, account_id) DO UPDATE SET
                    summary = excluded.summary,
                    keywords = excluded.keywords,
                    sentiment = excluded.sentiment,
                    action_items = excluded.action_items
                """,
                row,
            )
            conn.commit()

    def get_transcript(self, session_id: str, account_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM call_transcripts WHERE session_id = ? AND account_id = ? LIMIT 1",
                (session_id, account_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def get_summary(self, session_id: str, account_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM call_summaries WHERE session_id = ? AND account_id = ? LIMIT 1",
                (session_id, account_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def upsert_turns(
        self,
        *,
        session_id: str,
        account_id: str,
        turns: list[dict[str, Any]],
    ) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM call_transcript_turns WHERE session_id = ? AND account_id = ?",
                (session_id, account_id),
            )
            for turn_index, turn in enumerate(turns):
                speaker_role = str(turn.get("speaker_role") or turn.get("speaker") or "unknown").strip().lower() or "unknown"
                speaker_label = str(turn.get("speaker_label") or "").strip() or None
                text = " ".join(str(turn.get("text") or "").split()).strip()
                if not text:
                    continue
                conn.execute(
                    """
                    INSERT INTO call_transcript_turns(
                        id, session_id, account_id, turn_index, speaker_label, speaker_role, text,
                        timestamp_ms,
                        start_offset_ms, end_offset_ms, confidence, source_name, created_at
                    )
                    VALUES(
                        :id, :session_id, :account_id, :turn_index, :speaker_label, :speaker_role, :text,
                        :timestamp_ms,
                        :start_offset_ms, :end_offset_ms, :confidence, :source_name, :created_at
                    )
                    """,
                    {
                        "id": str(uuid4()),
                        "session_id": session_id,
                        "account_id": account_id,
                        "turn_index": turn_index,
                        "speaker_label": speaker_label,
                        "speaker_role": speaker_role,
                        "text": text,
                        "timestamp_ms": turn.get("timestamp_ms") if turn.get("timestamp_ms") is not None else turn.get("start_offset_ms"),
                        "start_offset_ms": turn.get("start_offset_ms"),
                        "end_offset_ms": turn.get("end_offset_ms"),
                        "confidence": turn.get("confidence"),
                        "source_name": str(turn.get("source_name") or "").strip() or None,
                        "created_at": now_iso,
                    },
                )
            conn.commit()

    def get_turns(self, session_id: str, account_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM call_transcript_turns
                WHERE session_id = ? AND account_id = ?
                ORDER BY turn_index ASC
                """,
                (session_id, account_id),
            ).fetchall()
        return [dict(row) for row in rows]


class GoogleSpeechV2Transcriber:
    """Google Cloud Speech-to-Text v2 adapter for saved recording files."""

    def __init__(
        self,
        *,
        project_id: str | None,
        recognizer_name: str | None = None,
        location: str = "global",
        recognizer_id: str = "_",
        model: str = "chirp_3",
        language_codes: list[str] | None = None,
    ) -> None:
        self.project_id = str(project_id or "").strip() or None
        self.recognizer_name = str(recognizer_name or "").strip() or None
        self.location = str(location or "global").strip() or "global"
        self.recognizer_id = str(recognizer_id or "_").strip() or "_"
        self.model = str(model or "chirp_3").strip() or "chirp_3"
        self.language_codes = [code.strip() for code in (language_codes or ["en-IN", "hi-IN", "mr-IN"]) if code.strip()]
        self.enable_diarization = True
        self.diarization_speaker_count = 2
        self._client = None
        self._cloud_speech = None
        self._import_error: Exception | None = None

    def _load_client(self) -> tuple[Any, Any]:
        if self._client is not None and self._cloud_speech is not None:
            return self._client, self._cloud_speech
        try:
            from google.cloud import speech_v2
            from google.cloud.speech_v2.types import cloud_speech
            from google.oauth2 import service_account
        except Exception as exc:  # pragma: no cover - dependency guard
            self._import_error = exc
            raise RuntimeError(
                "google-cloud-speech is not installed. Install google-cloud-speech to enable post-call transcription."
            ) from exc

        credentials = None
        credentials_path = str(os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "") or "").strip()
        credentials_json = str(os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON", "") or "").strip()
        if credentials_path:
            path = Path(credentials_path).expanduser()
            if not path.exists() or not path.is_file():
                raise RuntimeError(f"Google credentials file not found: {path}")
            credentials = service_account.Credentials.from_service_account_file(
                str(path),
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            if self.project_id is None and getattr(credentials, "project_id", None):
                self.project_id = str(credentials.project_id).strip() or None
        elif credentials_json:
            try:
                info = json.loads(credentials_json)
            except json.JSONDecodeError as exc:
                raise RuntimeError("GOOGLE_APPLICATION_CREDENTIALS_JSON is not valid JSON.") from exc
            credentials = service_account.Credentials.from_service_account_info(
                info,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            if self.project_id is None and isinstance(info, dict):
                self.project_id = str(info.get("project_id") or "").strip() or None

        if credentials is not None:
            self._client = speech_v2.SpeechClient(credentials=credentials)
        else:
            self._client = speech_v2.SpeechClient()
        self._cloud_speech = cloud_speech
        return self._client, cloud_speech

    async def transcribe(self, recording_path: Path) -> tuple[str, dict[str, Any]]:
        return await asyncio.to_thread(self._transcribe_sync, recording_path)

    def _build_recognition_config(self, cloud_speech: Any) -> Any:
        features = cloud_speech.RecognitionFeatures(
            enable_word_time_offsets=True,
            enable_automatic_punctuation=True,
        )
        if self.enable_diarization:
            features.diarization_config = cloud_speech.SpeakerDiarizationConfig(
                min_speaker_count=self.diarization_speaker_count,
                max_speaker_count=self.diarization_speaker_count,
            )
        config = cloud_speech.RecognitionConfig(
            auto_decoding_config=cloud_speech.AutoDetectDecodingConfig(),
            language_codes=self.language_codes or ["en-IN"],
            features=features,
        )
        if not self.recognizer_name:
            config.model = self.model
        return config

    @staticmethod
    def _duration_to_ms(value: Any) -> int | None:
        if value in (None, ""):
            return None
        seconds = getattr(value, "seconds", None)
        nanos = getattr(value, "nanos", None)
        if seconds is not None or nanos is not None:
            try:
                total = float(seconds or 0) + float(nanos or 0) / 1_000_000_000.0
                return int(round(total * 1000))
            except (TypeError, ValueError):
                return None
        with contextlib.suppress(TypeError, ValueError):
            return int(round(float(value) * 1000))
        return None

    def _extract_best_alternative(self, response: Any) -> Any | None:
        best = None
        for result in getattr(response, "results", []) or []:
            alternatives = list(getattr(result, "alternatives", []) or [])
            if alternatives:
                best = alternatives[0]
        return best

    def _build_turns_from_words(self, words: list[Any]) -> list[dict[str, Any]]:
        turns: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        for word in words:
            token = str(getattr(word, "word", "") or "").strip()
            if not token:
                continue
            speaker_label = str(getattr(word, "speaker_label", "") or "").strip() or "unknown"
            start_ms = self._duration_to_ms(getattr(word, "start_offset", None))
            end_ms = self._duration_to_ms(getattr(word, "end_offset", None))
            confidence = getattr(word, "confidence", None)
            if current and current.get("speaker_label") == speaker_label:
                current["text"] = f"{current['text']} {token}".strip()
                if end_ms is not None:
                    current["end_offset_ms"] = end_ms
                if confidence not in (None, ""):
                    with contextlib.suppress(TypeError, ValueError):
                        current["confidence"] = max(float(current.get("confidence") or 0.0), float(confidence))
                continue
            if current and current.get("text"):
                turns.append(current)
            current = {
                "speaker_label": speaker_label,
                "text": token,
                "start_offset_ms": start_ms,
                "end_offset_ms": end_ms,
                "confidence": float(confidence) if confidence not in (None, "") else None,
            }
        if current and current.get("text"):
            turns.append(current)
        return turns

    def _parse_response(self, response: Any) -> tuple[str, list[float], str | None, list[dict[str, Any]]]:
        transcripts: list[str] = []
        confidence_values: list[float] = []
        best = self._extract_best_alternative(response)
        words = list(getattr(best, "words", []) or []) if best is not None else []
        if best is not None:
            text = str(getattr(best, "transcript", "") or "").strip()
            if text:
                transcripts.append(text)
            confidence = getattr(best, "confidence", None)
            if confidence not in (None, ""):
                with contextlib.suppress(TypeError, ValueError):
                    confidence_values.append(float(confidence))
        if not transcripts and words:
            joined_words = " ".join(
                str(getattr(word, "word", "") or "").strip()
                for word in words
                if str(getattr(word, "word", "") or "").strip()
            ).strip()
            if joined_words:
                transcripts.append(joined_words)
        turns = self._build_turns_from_words(words)
        return " ".join(transcripts).strip(), confidence_values, self._resolve_language_hint(response), turns

    def _recognize_audio_content(
        self,
        *,
        client: Any,
        cloud_speech: Any,
        recognizer: str,
        audio_content: bytes,
    ) -> tuple[str, dict[str, Any]]:
        config = self._build_recognition_config(cloud_speech)
        request = cloud_speech.RecognizeRequest(recognizer=recognizer, config=config, content=audio_content)
        response = self._recognize_with_fallbacks(client, request, cloud_speech)
        transcript_text, confidence_values, language, speaker_turns = self._parse_response(response)
        if speaker_turns:
            transcript_text = "\n".join(
                f"Speaker {turn['speaker_label']}: {turn['text']}"
                for turn in speaker_turns
                if str(turn.get("text") or "").strip()
            ).strip()
        metadata = {
            "language": language,
            "confidence": round(mean(confidence_values), 4) if confidence_values else None,
            "model": str(getattr(response, "model", "") or (self.model if not self.recognizer_name else "")),
            "provider": "google_cloud_speech_v2",
            "speaker_turns": speaker_turns,
        }
        return transcript_text, metadata

    def _should_chunk_retry(self, exc: Exception) -> bool:
        message = str(exc)
        return "maximum of 60 seconds" in message or "Audio can be of a maximum of 60 seconds" in message

    def _transcribe_wav_in_chunks(
        self,
        *,
        client: Any,
        cloud_speech: Any,
        recognizer: str,
        recording_path: Path,
    ) -> tuple[str, dict[str, Any]]:
        chunk_seconds = 55
        transcript_parts: list[str] = []
        speaker_turns: list[dict[str, Any]] = []
        confidence_values: list[float] = []
        languages: list[str] = []
        chunk_offset_ms = 0

        with wave.open(str(recording_path), "rb") as source:
            channels = source.getnchannels()
            sample_width = source.getsampwidth()
            frame_rate = source.getframerate()
            total_frames = source.getnframes()
            if frame_rate <= 0 or total_frames <= 0:
                return "", {"language": None, "confidence": None, "model": self.model, "provider": "google_cloud_speech_v2"}
            chunk_frames = max(int(frame_rate * chunk_seconds), frame_rate)
            while True:
                frames = source.readframes(chunk_frames)
                if not frames:
                    break
                with tempfile.NamedTemporaryFile(prefix="recording_stt_chunk_", suffix=".wav", delete=False) as temp_file:
                    temp_path = Path(temp_file.name)
                try:
                    with wave.open(str(temp_path), "wb") as out:
                        out.setnchannels(channels)
                        out.setsampwidth(sample_width)
                        out.setframerate(frame_rate)
                        out.writeframes(frames)
                    chunk_text, chunk_meta = self._recognize_audio_content(
                        client=client,
                        cloud_speech=cloud_speech,
                        recognizer=recognizer,
                        audio_content=temp_path.read_bytes(),
                    )
                    if chunk_text:
                        transcript_parts.append(chunk_text)
                    chunk_turns = list(chunk_meta.get("speaker_turns") or [])
                    for turn in chunk_turns:
                        start_ms = turn.get("start_offset_ms")
                        end_ms = turn.get("end_offset_ms")
                        if start_ms is not None:
                            turn["start_offset_ms"] = int(start_ms) + chunk_offset_ms
                        if end_ms is not None:
                            turn["end_offset_ms"] = int(end_ms) + chunk_offset_ms
                        speaker_turns.append(turn)
                    confidence = chunk_meta.get("confidence")
                    if confidence not in (None, ""):
                        with contextlib.suppress(TypeError, ValueError):
                            confidence_values.append(float(confidence))
                    language = str(chunk_meta.get("language") or "").strip()
                    if language:
                        languages.append(language)
                finally:
                    with contextlib.suppress(Exception):
                        temp_path.unlink(missing_ok=True)
                chunk_offset_ms += int(round(chunk_seconds * 1000))

        transcript_text = " ".join(transcript_parts).strip()
        metadata = {
            "language": languages[0] if languages else None,
            "confidence": round(mean(confidence_values), 4) if confidence_values else None,
            "model": self.model if not self.recognizer_name else "",
            "provider": "google_cloud_speech_v2",
            "mode": "chunked_55s",
            "speaker_turns": speaker_turns,
        }
        return transcript_text, metadata

    def _transcribe_sync(self, recording_path: Path) -> tuple[str, dict[str, Any]]:
        client, cloud_speech = self._load_client()
        if self.project_id is None:
            raise RuntimeError("Missing Google STT project id. Set GOOGLE_STT_PROJECT_ID or GOOGLE_CLOUD_PROJECT.")
        recognizer = self.recognizer_name or f"projects/{self.project_id}/locations/{self.location}/recognizers/{self.recognizer_id}"
        audio_content = recording_path.read_bytes()
        try:
            return self._recognize_audio_content(
                client=client,
                cloud_speech=cloud_speech,
                recognizer=recognizer,
                audio_content=audio_content,
            )
        except Exception as exc:
            if self._should_chunk_retry(exc):
                with contextlib.suppress(Exception):
                    with wave.open(str(recording_path), "rb"):
                        pass
                try:
                    return self._transcribe_wav_in_chunks(
                        client=client,
                        cloud_speech=cloud_speech,
                        recognizer=recognizer,
                        recording_path=recording_path,
                    )
                except Exception:
                    raise exc
            raise

    def _recognize_with_fallbacks(self, client: Any, request: Any, cloud_speech: Any) -> Any:
        try:
            return client.recognize(request=request)
        except Exception as exc:
            message = str(exc)
            if self.recognizer_name:
                raise
            if "chirp_3" in message and "global" in message:
                fallback_config = cloud_speech.RecognitionConfig(
                    auto_decoding_config=cloud_speech.AutoDetectDecodingConfig(),
                    language_codes=self.language_codes or ["en-IN"],
                    model="latest_long",
                )
                fallback_request = cloud_speech.RecognizeRequest(
                    recognizer=request.recognizer,
                    config=fallback_config,
                    content=request.content,
                )
                return client.recognize(request=fallback_request)
            raise

    @staticmethod
    def _resolve_language_hint(response: Any) -> str | None:
        for result in getattr(response, "results", []) or []:
            for alternative in getattr(result, "alternatives", []) or []:
                language = str(getattr(alternative, "language_code", "") or "").strip()
                if language:
                    return language
        return None


class FasterWhisperRecordingTranscriber:
    """Post-call recording transcriber backed by Faster-Whisper."""

    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        self._transcriber = FasterWhisperTranscriber(settings)

    @property
    def model(self) -> str:
        return self._settings.faster_stt_model

    @property
    def enabled(self) -> bool:
        return self._transcriber.enabled

    async def transcribe(self, recording_path: Path) -> tuple[str, dict[str, Any]]:
        transcript_text, metadata = await self._transcriber.transcribe_wav_best_async(
            recording_path,
            ["hindi", "marathi", None],
            chunk_seconds=15,
        )
        transcript_text = " ".join(str(transcript_text or "").split()).strip()
        return transcript_text, {
            "language": metadata.get("language"),
            "confidence": None,
            "model": metadata.get("model") or self._settings.faster_stt_model,
            "provider": "faster_whisper",
            "strategy": metadata.get("strategy"),
            "language_hint": metadata.get("language_hint"),
            "speaker_turns": [],
        }


class GeminiAudioRecordingTranscriber:
    """Post-call recording transcriber backed by Gemini audio understanding."""

    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        self._client = genai.Client(api_key=settings.gemini_api_key)
        self._model = str(getattr(settings, "recording_audio_model", "gemini-2.5-flash") or "gemini-2.5-flash")
        self._fallback = FasterWhisperTranscriber(settings)

    @property
    def model(self) -> str:
        return self._model

    @property
    def enabled(self) -> bool:
        return True

    @staticmethod
    def _normalize_text(text: str | None) -> str:
        return " ".join(str(text or "").split()).strip()

    @staticmethod
    def _prompt() -> str:
        return (
            "Transcribe this call recording exactly. Preserve the spoken language and do not translate, summarize, "
            "or add commentary. If the conversation has multiple speakers, keep separate paragraphs for each turn. "
            "Return only the transcript text."
        )

    def _mime_type(self, recording_path: Path) -> str:
        suffix = recording_path.suffix.lower()
        if suffix in {".wav"}:
            return "audio/wav"
        if suffix in {".mp3", ".mpeg"}:
            return "audio/mpeg"
        return "application/octet-stream"

    def _transcribe_sync(self, recording_path: Path) -> tuple[str, dict[str, Any]]:
        if not recording_path.exists():
            return "", {
                "language": None,
                "confidence": None,
                "model": self.model,
                "provider": "gemini_audio",
                "strategy": "missing_file",
                "speaker_turns": [],
            }
        try:
            uploaded = self._client.files.upload(file=str(recording_path))
            response = self._client.models.generate_content(
                model=self._model,
                contents=[
                    types.Part.from_uri(file_uri=uploaded.uri, mime_type=uploaded.mime_type or self._mime_type(recording_path)),
                    types.Part.from_text(text=self._prompt()),
                ],
                config=types.GenerateContentConfig(
                    temperature=0.0,
                    response_mime_type="text/plain",
                ),
            )
            transcript_text = self._normalize_text(response.text)
            if transcript_text:
                return transcript_text, {
                    "language": None,
                    "confidence": None,
                    "model": self.model,
                    "provider": "gemini_audio",
                    "strategy": "gemini_audio",
                    "speaker_turns": [],
                }
        except Exception as exc:
            logger.warning("Gemini audio transcription failed for %s: %s", recording_path, exc)

        fallback_text, fallback_meta = self._fallback.transcribe_wav_best(
            recording_path,
            ["hindi", "marathi", None],
            chunk_seconds=15,
        )
        fallback_text = self._normalize_text(fallback_text)
        return fallback_text, {
            "language": fallback_meta.get("language"),
            "confidence": fallback_meta.get("language_probability"),
            "model": fallback_meta.get("model") or self._fallback._settings.faster_stt_model,
            "provider": "faster_whisper",
            "strategy": f"fallback:{fallback_meta.get('strategy') or 'whisper'}",
            "language_hint": fallback_meta.get("language_hint"),
            "speaker_turns": [],
        }

    async def transcribe(self, recording_path: Path) -> tuple[str, dict[str, Any]]:
        return await asyncio.to_thread(self._transcribe_sync, recording_path)

    async def transcribe_wav_async(self, wav_path: Path, language_hint: str | None = None) -> str:
        text, _metadata = await self.transcribe(wav_path)
        return text

    async def transcribe_wav_best_async(
        self,
        wav_path: Path,
        language_hints: list[str | None] | None = None,
        chunk_seconds: int = 15,
    ) -> tuple[str, dict[str, Any]]:
        _ = language_hints, chunk_seconds
        return await self.transcribe(wav_path)


def build_transcript_summary(transcript_text: str) -> dict[str, Any]:
    """Create a deterministic summary when we do not want to call Gemini."""
    normalized = " ".join(str(transcript_text or "").split()).strip()
    if not normalized:
        return {
            "summary": "No transcript text was produced.",
            "keywords": [],
            "sentiment": "neutral",
            "action_items": [],
        }

    sentences = [segment.strip() for segment in re.split(r"(?<=[.!?])\s+", normalized) if segment.strip()]
    first_sentences = sentences[:2] if sentences else [normalized[:220]]
    summary = " ".join(first_sentences).strip()
    if len(summary) > 360:
        summary = f"{summary[:357].rstrip()}..."

    tokens = [token.lower() for token in re.findall(r"\b[\w']{4,}\b", normalized)]
    stopwords = {
        "that",
        "with",
        "this",
        "from",
        "have",
        "will",
        "your",
        "about",
        "there",
        "their",
        "please",
        "would",
        "could",
        "should",
        "calls",
        "call",
        "said",
        "were",
        "they",
        "them",
        "what",
        "when",
        "where",
        "which",
        "into",
        "more",
        "than",
        "been",
    }
    freq = Counter(token for token in tokens if token not in stopwords)
    keywords = [word for word, _ in freq.most_common(8)]

    positive_words = {"good", "great", "thanks", "thank", "yes", "interested", "book", "confirm", "helpful", "okay"}
    negative_words = {"no", "not", "problem", "busy", "later", "cancel", "can't", "cannot", "difficult", "issue"}
    pos_score = sum(1 for token in tokens if token in positive_words)
    neg_score = sum(1 for token in tokens if token in negative_words)
    sentiment = "positive" if pos_score > neg_score else "negative" if neg_score > pos_score else "neutral"

    action_items: list[str] = []
    action_markers = (
        "follow up",
        "call back",
        "send",
        "share",
        "confirm",
        "book",
        "schedule",
        "whatsapp",
        "visit",
        "meet",
        "email",
    )
    for sentence in sentences:
        lowered = sentence.lower()
        if any(marker in lowered for marker in action_markers):
            action_items.append(sentence[:220])
    if not action_items and summary:
        action_items.append(summary[:220])

    return {
        "summary": summary,
        "keywords": keywords,
        "sentiment": sentiment,
        "action_items": action_items[:5],
    }


class PostCallTranscriptionPipeline:
    """Queue-based background worker for post-call transcription jobs."""

    def __init__(
        self,
        *,
        settings: AppSettings,
        store: SqliteCallTranscriptionStore,
        telephony: Any | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.telephony = telephony
        self._structured_client = genai.Client(api_key=settings.gemini_api_key)
        self._structured_model = str(getattr(settings, "structured_model", "") or "gemini-2.5-flash")
        self.transcriber = FasterWhisperRecordingTranscriber(settings)
        self.summary_enabled = bool(getattr(settings, "post_call_summary_enabled", True))
        self.whatsapp_enabled = bool(getattr(settings, "post_call_whatsapp_notification_enabled", True))
        self.whatsapp_default_number = (
            str(getattr(settings, "post_call_whatsapp_number", "") or "").strip() or "+919370677316"
        )
        self.whatsapp_template_name = str(
            getattr(settings, "meta_whatsapp_post_call_template_name", "") or ""
        ).strip() or None
        self.whatsapp_template_language_code = str(
            getattr(settings, "meta_whatsapp_post_call_template_language_code", "en_US") or "en_US"
        ).strip() or "en_US"
        self._queue: asyncio.Queue[RecordingTranscriptionJob] = asyncio.Queue()
        self._enqueued_jobs: set[tuple[str, str]] = set()
        self._worker_task: asyncio.Task[None] | None = None
        self._running = False

    @staticmethod
    def _speaker_role_from_hint(recording_type: str | None, speaker_label: str, label_order: list[str]) -> str:
        normalized_type = str(recording_type or "").strip().lower()
        raw_label = str(speaker_label or "").strip() or "unknown"
        ordered = [str(label or "").strip() for label in label_order if str(label or "").strip()]
        first_label = ordered[0] if ordered else raw_label
        second_label = ordered[1] if len(ordered) > 1 else None
        if normalized_type == "caller_leg":
            if raw_label == first_label:
                return "user"
            if second_label and raw_label == second_label:
                return "ai"
            return "user"
        if normalized_type in {"ai_leg", "full_or_unknown", "unknown"}:
            if raw_label == first_label:
                return "ai"
            if second_label and raw_label == second_label:
                return "user"
            return "ai"
        return raw_label

    @staticmethod
    def _prefix_transcript_turns(turns: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for turn in turns:
            role = str(turn.get("speaker_role") or "").strip().lower()
            speaker = "AI" if role == "ai" else "User" if role == "user" else str(turn.get("speaker_label") or "unknown").strip() or "unknown"
            text = " ".join(str(turn.get("text") or "").split()).strip()
            if text:
                lines.append(f"{speaker}: {text}")
        return "\n".join(lines).strip()

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._worker_task = asyncio.create_task(self._worker_loop(), name="post_call_transcription_worker")

    async def stop(self) -> None:
        self._running = False
        if self._worker_task is not None:
            self._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._worker_task
        self._worker_task = None

    async def enqueue(self, job: RecordingTranscriptionJob) -> None:
        if str(job.recording_status or "").strip().lower() != "saved":
            return
        job_key = (job.account_id, job.session_id)
        if job_key in self._enqueued_jobs:
            return
        self._enqueued_jobs.add(job_key)
        await self._queue.put(job)

    async def recover_pending_jobs(self, session_root: Path) -> int:
        recovered = 0
        if not session_root.exists():
            return recovered
        for account_dir in sorted((item for item in session_root.iterdir() if item.is_dir()), key=lambda item: item.name):
            for session_dir in sorted((item for item in account_dir.iterdir() if item.is_dir()), key=lambda item: item.name):
                meta_path = session_dir / "piopiy_recording.json"
                if not meta_path.exists():
                    continue
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if not isinstance(meta, dict):
                    continue
                if str(meta.get("recording_status") or "").strip().lower() != "saved":
                    continue
                transcript_path = session_dir / "post_call_transcription.json"
                if transcript_path.exists():
                    continue
                await self.enqueue(
                    RecordingTranscriptionJob(
                        account_id=account_dir.name,
                        session_id=session_dir.name,
                        caller_number=str(meta.get("caller_number") or meta.get("from_number") or "").strip() or None,
                        recording_path=str(meta.get("recording_path") or session_dir / str(meta.get("recording_filename") or "")),
                        recording_status="saved",
                        language_hint=str(meta.get("language") or "").strip() or None,
                    )
                )
                recovered += 1
        return recovered

    async def _worker_loop(self) -> None:
        while self._running:
            job = await self._queue.get()
            try:
                await self._process_job(job)
            except Exception as exc:  # pragma: no cover - defensive logging path
                logger.exception(
                    "Post-call transcription job failed account_id=%s session_id=%s error=%s",
                    job.account_id,
                    job.session_id,
                    exc,
                )
                self._update_sidecar(
                    Path(job.recording_path).parent / "post_call_transcription.json",
                    {
                        "account_id": job.account_id,
                        "session_id": job.session_id,
                        "recording_path": job.recording_path,
                        "transcription_status": "failed",
                        "error": str(exc),
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
            finally:
                self._enqueued_jobs.discard((job.account_id, job.session_id))
                self._queue.task_done()

    async def _process_job(self, job: RecordingTranscriptionJob) -> None:
        recording_path = Path(job.recording_path).expanduser().resolve()
        if job.recording_status != "saved":
            return
        if not recording_path.exists() or not recording_path.is_file():
            raise FileNotFoundError(f"Recording missing: {recording_path}")

        started_at = datetime.now(timezone.utc).isoformat()
        self.store.upsert_transcript(
            session_id=job.session_id,
            account_id=job.account_id,
            caller_number=job.caller_number,
            recording_path=str(recording_path),
            transcript_text="",
            language=job.language_hint,
            transcription_provider="google_cloud_speech_v2",
            transcription_model=self.transcriber.model,
            confidence=None,
            transcription_status="processing",
            started_at=started_at,
            completed_at=None,
        )
        self._update_call_context(job, {"transcription_status": "processing", "transcription_started_at": started_at})

        transcript_text, metadata = await self.transcriber.transcribe(recording_path)
        raw_turns = list(metadata.get("speaker_turns") or [])
        recording_meta_path = recording_path.parent / "piopiy_recording.json"
        recording_meta = _read_json_file(recording_meta_path)
        recording_type = ""
        if isinstance(recording_meta, dict):
            recording_type = str(recording_meta.get("selected_recording_type") or recording_meta.get("cdr_leg") or "").strip().lower()
        speaker_label_order: list[str] = []
        for turn in raw_turns:
            if not isinstance(turn, dict):
                continue
            label = str(turn.get("speaker_label") or "").strip()
            if label and label not in speaker_label_order:
                speaker_label_order.append(label)
        if raw_turns:
            mapped_turns: list[dict[str, Any]] = []
            for turn in raw_turns:
                if not isinstance(turn, dict):
                    continue
                text = " ".join(str(turn.get("text") or "").split()).strip()
                if not text:
                    continue
                speaker_label = str(turn.get("speaker_label") or "").strip() or "unknown"
                speaker_role = self._speaker_role_from_hint(recording_type, speaker_label, speaker_label_order)
                mapped_turns.append(
                    {
                        "speaker_label": speaker_label,
                        "speaker_role": speaker_role,
                        "text": text,
                        "start_offset_ms": turn.get("start_offset_ms"),
                        "end_offset_ms": turn.get("end_offset_ms"),
                        "confidence": turn.get("confidence"),
                        "source_name": recording_meta_path.name,
                    }
                )
            if mapped_turns:
                transcript_text = self._prefix_transcript_turns(mapped_turns)
                self.store.upsert_turns(
                    session_id=job.session_id,
                    account_id=job.account_id,
                    turns=mapped_turns,
                )
                metadata["speaker_turns"] = mapped_turns
        confidence = metadata.get("confidence")
        language = metadata.get("language") or job.language_hint
        completed_at = datetime.now(timezone.utc).isoformat()
        transcription_status = "completed" if transcript_text else "completed_empty"

        self.store.upsert_transcript(
            session_id=job.session_id,
            account_id=job.account_id,
            caller_number=job.caller_number,
            recording_path=str(recording_path),
            transcript_text=transcript_text,
            language=str(language or "").strip() or None,
            transcription_provider=str(metadata.get("provider") or "google_cloud_speech_v2"),
            transcription_model=str(metadata.get("model") or self.transcriber.model),
            confidence=float(confidence) if confidence is not None else None,
            transcription_status=transcription_status,
            started_at=started_at,
            completed_at=completed_at,
        )

        caller_transcript_text = self._build_caller_only_transcript(metadata, transcript_text)
        summary_payload = build_transcript_summary(transcript_text) if self.summary_enabled else {
            "summary": "",
            "keywords": [],
            "sentiment": "neutral",
            "action_items": [],
        }
        key_details = (
            await self._extract_key_details(job.session_id, caller_transcript_text)
            if caller_transcript_text
            else RecordingKeyDetails()
        )
        key_details = clear_non_caller_location(key_details, caller_transcript_text)
        if self.summary_enabled:
            self.store.upsert_summary(
                session_id=job.session_id,
                account_id=job.account_id,
                summary=str(summary_payload.get("summary") or "").strip(),
                keywords=list(summary_payload.get("keywords") or []),
                sentiment=str(summary_payload.get("sentiment") or "neutral").strip() or "neutral",
                action_items=list(summary_payload.get("action_items") or []),
            )

        self._update_call_context(
            job,
            {
                "transcription_status": transcription_status,
                "transcription_started_at": started_at,
                "transcription_completed_at": completed_at,
                "transcription_provider": str(metadata.get("provider") or "google_cloud_speech_v2"),
                "transcription_model": str(metadata.get("model") or self.transcriber.model),
                "transcription_language": str(language or "").strip() or None,
                "transcription_confidence": confidence,
                "transcript_text": transcript_text or None,
                "transcription_summary": summary_payload.get("summary") if self.summary_enabled else None,
                "recording_key_details": key_details.model_dump(mode="json") if key_details else None,
            },
        )
        self._update_sidecar(
            recording_path.parent / "post_call_transcription.json",
            {
                "account_id": job.account_id,
                "session_id": job.session_id,
                "caller_number": job.caller_number,
                "recording_path": str(recording_path),
                "transcript_text": transcript_text,
                "language": language,
                "transcription_provider": str(metadata.get("provider") or "google_cloud_speech_v2"),
                "transcription_model": str(metadata.get("model") or self.transcriber.model),
                "confidence": confidence,
                "transcription_status": transcription_status,
                "transcription_started_at": started_at,
                "transcription_completed_at": completed_at,
                "summary": summary_payload.get("summary") if self.summary_enabled else None,
                "keywords": summary_payload.get("keywords") if self.summary_enabled else [],
                "sentiment": summary_payload.get("sentiment") if self.summary_enabled else "neutral",
                "action_items": summary_payload.get("action_items") if self.summary_enabled else [],
                "key_details": key_details.model_dump(mode="json") if key_details else None,
                "speaker_turns": metadata.get("speaker_turns") if metadata.get("speaker_turns") else [],
                "updated_at": completed_at,
            },
        )
        logger.info(
            "Post-call transcription completed account_id=%s session_id=%s confidence=%s",
            job.account_id,
            job.session_id,
            confidence,
        )

        if self.whatsapp_enabled:
            recipient = self._resolve_whatsapp_recipient(job)
            if recipient:
                await self._send_whatsapp_notification(job, recipient, key_details, confidence)

    def _resolve_whatsapp_recipient(self, job: RecordingTranscriptionJob) -> str | None:
        if job.notify_whatsapp_number:
            return job.notify_whatsapp_number
        return self.whatsapp_default_number

    def _build_whatsapp_notification(
        self,
        job: RecordingTranscriptionJob,
        key_details: RecordingKeyDetails,
        confidence: float | None,
    ) -> str:
        parts = [
            f"Call details for {job.account_id}/{job.session_id}",
            f"Confidence: {round(confidence * 100, 1)}%" if confidence is not None else "Confidence: n/a",
            f"Name: {key_details.name or 'Not mentioned'}",
            f"Problem: {key_details.problem or 'Not mentioned'}",
            f"Location: {key_details.location or 'Not mentioned'}",
        ]
        return "\n".join(parts)

    def _build_whatsapp_template_params(
        self,
        job: RecordingTranscriptionJob,
        key_details: RecordingKeyDetails,
        confidence: float | None,
    ) -> list[str]:
        return [
            key_details.name or "Not mentioned",
            self._clean_whatsapp_number(job.caller_number or "") or "-",
            key_details.problem or "Not mentioned",
            key_details.location or "-",
        ]

    @staticmethod
    def _clean_whatsapp_number(to_number: str) -> str:
        normalized = re.sub(r"\D+", "", str(to_number or ""))
        return normalized

    @staticmethod
    def _build_caller_only_transcript(metadata: dict[str, Any], transcript_text: str) -> str:
        speaker_turns = metadata.get("speaker_turns") if isinstance(metadata, dict) else None
        caller_lines: list[str] = []
        if isinstance(speaker_turns, list):
            for turn in speaker_turns:
                if not isinstance(turn, dict):
                    continue
                speaker_role = str(turn.get("speaker_role") or turn.get("speaker") or "").strip().lower()
                text = " ".join(str(turn.get("text") or "").split()).strip()
                if not text:
                    continue
                if speaker_role in {"user", "caller"}:
                    caller_lines.append(text)
        if caller_lines:
            return "\n".join(caller_lines).strip()

        fallback_lines: list[str] = []
        for line in str(transcript_text or "").splitlines():
            cleaned = " ".join(line.split()).strip()
            if not cleaned:
                continue
            lowered = cleaned.lower()
            if lowered.startswith("user:") or lowered.startswith("caller:"):
                fallback_lines.append(cleaned.split(":", 1)[1].strip())
        if fallback_lines:
            return "\n".join(fallback_lines).strip()
        return ""

    async def _extract_key_details(self, session_id: str, transcript_text: str) -> RecordingKeyDetails:
        prompt = build_key_details_prompt(transcript_text)
        last_error: Exception | None = None
        for attempt in range(1, 3):
            try:
                response = await asyncio.to_thread(
                    self._structured_client.models.generate_content,
                    model=self._structured_model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.1,
                        top_p=0.8,
                        max_output_tokens=256,
                    ),
                )
                text = (response.text or "").strip()
                if text:
                    details = parse_key_details_payload(text)
                    if details.name or details.problem or details.location:
                        return details
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Key detail extraction failed session_id=%s attempt=%s/2 error=%s",
                    session_id,
                    attempt,
                    exc,
                )
            if attempt < 2:
                await asyncio.sleep(0.5 * attempt)
        if last_error is not None:
            logger.debug("Key detail extraction fallback used after error: %s", last_error)
        return RecordingKeyDetails()

    async def _send_whatsapp_notification(
        self,
        job: RecordingTranscriptionJob,
        to_number: str,
        key_details: RecordingKeyDetails,
        confidence: float | None,
    ) -> None:
        if self.telephony is None:
            return
        meta_whatsapp = getattr(self.telephony, "meta_whatsapp", None)
        if meta_whatsapp is None:
            return
        try:
            normalized_to_number = self._clean_whatsapp_number(to_number)
            if not normalized_to_number:
                return
            if self.whatsapp_template_name:
                await meta_whatsapp.send_template_message(
                    to_number=normalized_to_number,
                    template_name=self.whatsapp_template_name,
                    language_code=self.whatsapp_template_language_code,
                    body_params=self._build_whatsapp_template_params(
                        job,
                        key_details,
                        confidence,
                    ),
                )
            else:
                body_text = self._build_whatsapp_notification(
                    job,
                    key_details,
                    confidence,
                )
                await meta_whatsapp.send_text_message(to_number=normalized_to_number, body_text=body_text)
        except Exception as exc:  # pragma: no cover - external API failure path
            logger.warning("WhatsApp transcription notification failed to=%s error=%s", to_number, exc)

    def _update_call_context(self, job: RecordingTranscriptionJob, updates: dict[str, Any]) -> None:
        if self.telephony is None:
            return
        update_call_context = getattr(self.telephony, "update_call_context", None)
        if update_call_context is None:
            return

        async def _runner() -> None:
            try:
                await update_call_context(job.session_id, updates)
            except Exception:
                logger.debug("Failed to update call context for transcription session_id=%s", job.session_id)

        with contextlib.suppress(RuntimeError):
            asyncio.create_task(_runner())

    @staticmethod
    def _update_sidecar(path: Path, payload: dict[str, Any]) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        except Exception as exc:  # pragma: no cover - filesystem failure path
            logger.debug("Failed to write transcription sidecar path=%s error=%s", path, exc)
