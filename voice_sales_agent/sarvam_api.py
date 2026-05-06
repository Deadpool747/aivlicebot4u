"""Sarvam API wrappers for TTS and file-based STT."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from pathlib import Path
from array import array
import io
from typing import Any
import urllib.error
import urllib.request
import uuid
import wave

logger = logging.getLogger(__name__)


def _language_to_bcp47(language_hint: str | None, default_code: str = "en-IN") -> str:
    normalized = str(language_hint or "").strip().lower()
    if normalized in {"mr", "marathi", "mr-in"}:
        return "mr-IN"
    if normalized in {"hi", "hindi", "hi-in"}:
        return "hi-IN"
    if normalized in {"en", "english", "en-in"}:
        return "en-IN"
    return default_code


def _resample_linear_pcm16_mono(payload: bytes, src_rate: int, dst_rate: int) -> bytes:
    if not payload or src_rate <= 0 or dst_rate <= 0 or src_rate == dst_rate:
        return payload
    source = array("h")
    source.frombytes(payload)
    if not source:
        return b""
    if len(source) == 1:
        return array("h", [source[0]]).tobytes()
    target_length = max(1, int(len(source) * dst_rate / src_rate))
    target = array("h", [0] * target_length)
    step = (len(source) - 1) / max(target_length - 1, 1)
    for index in range(target_length):
        position = index * step
        left = int(position)
        right = min(left + 1, len(source) - 1)
        alpha = position - left
        value = int(source[left] * (1.0 - alpha) + source[right] * alpha)
        if value > 32767:
            value = 32767
        elif value < -32768:
            value = -32768
        target[index] = value
    return target.tobytes()


def _normalize_audio_for_pipeline(audio_bytes: bytes, target_rate: int = 24_000) -> bytes:
    """Return raw PCM16 mono at target sample rate for audio pipeline compatibility."""
    if not audio_bytes:
        return b""
    if audio_bytes[:4] != b"RIFF":
        return audio_bytes
    try:
        with wave.open(io.BytesIO(audio_bytes), "rb") as wav:
            channels = wav.getnchannels()
            sample_width = wav.getsampwidth()
            frame_rate = wav.getframerate()
            frames = wav.readframes(wav.getnframes())
        if sample_width != 2:
            return b""
        if channels == 2:
            stereo = array("h")
            stereo.frombytes(frames)
            mono = array("h")
            for i in range(0, len(stereo), 2):
                left = stereo[i]
                right = stereo[i + 1] if i + 1 < len(stereo) else left
                mono.append(int((left + right) / 2))
            frames = mono.tobytes()
        elif channels != 1:
            return b""
        if frame_rate != target_rate:
            frames = _resample_linear_pcm16_mono(frames, frame_rate, target_rate)
        return frames
    except Exception as exc:
        logger.warning("Failed to normalize Sarvam audio payload: %s", exc)
        return b""


class SarvamSpeechClient:
    """Generate speech via Sarvam text-to-speech REST API."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.sarvam.ai",
        model: str = "bulbul:v3",
        speaker: str = "shubh",
        target_language_code: str = "en-IN",
    ) -> None:
        self._api_key = api_key.strip()
        self._base_url = base_url.rstrip("/")
        self._model = model.strip() or "bulbul:v3"
        self._speaker = speaker.strip() or "shubh"
        self._target_language_code = target_language_code.strip() or "en-IN"
        self._cache: dict[tuple[str, str], bytes] = {}

    def _post_json(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url=f"{self._base_url}{path}",
            data=data,
            method="POST",
            headers={
                "api-subscription-key": self._api_key,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                raw = response.read().decode("utf-8")
                parsed = json.loads(raw) if raw else {}
                return parsed if isinstance(parsed, dict) else {}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Sarvam API error ({exc.code}) at {path}: {body[:300]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Sarvam API request failed at {path}: {exc.reason}") from exc

    async def synthesize(self, text: str, voice_name: str) -> bytes:
        """Synthesize one exact line of speech."""
        normalized_text = text.strip()
        cache_key = (voice_name, normalized_text)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        if not normalized_text:
            return b""
        payload = {
            "text": normalized_text,
            "model": self._model,
            "speaker": self._speaker,
            "target_language_code": self._target_language_code,
        }
        response = await asyncio.to_thread(self._post_json, "/text-to-speech", payload)
        audios = response.get("audios")
        if not isinstance(audios, list) or not audios:
            raise RuntimeError("Sarvam TTS returned no audio chunks.")
        joined = "".join(str(chunk) for chunk in audios if chunk)
        if not joined:
            raise RuntimeError("Sarvam TTS returned empty audio payload.")
        audio_bytes = _normalize_audio_for_pipeline(base64.b64decode(joined))
        self._cache[cache_key] = audio_bytes
        return audio_bytes


class SarvamStructuredClient:
    """Sarvam chat-completion wrapper for summaries and text generation."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.sarvam.ai",
        model: str = "sarvam-30b",
    ) -> None:
        self._api_key = api_key.strip()
        self._base_url = base_url.rstrip("/")
        self._model = model.strip() or "sarvam-30b"

    def _chat_complete(self, messages: list[dict[str, str]], temperature: float = 0.2, max_tokens: int = 2048) -> str:
        payload = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url=f"{self._base_url}/v1/chat/completions",
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "api-subscription-key": self._api_key,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read().decode("utf-8")
                payload = json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Sarvam chat API error ({exc.code}): {body_text[:300]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Sarvam chat API request failed: {exc.reason}") from exc

        choices = payload.get("choices") if isinstance(payload, dict) else None
        if not isinstance(choices, list) or not choices:
            return ""
        message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
        return str(message.get("content", "") or "").strip()

    async def summarize_call(self, client_bundle: Any, artifacts: Any) -> Any:
        from .extraction import build_extraction_prompt, parse_summary_payload

        prompt = build_extraction_prompt(client_bundle, artifacts)
        text = await asyncio.to_thread(
            self._chat_complete,
            [{"role": "user", "content": prompt}],
            0.2,
            2000,
        )
        return parse_summary_payload(text or "{}")

    async def generate_chat_reply(
        self,
        *,
        system_prompt: str,
        user_text: str,
        generation_settings: Any = None,
    ) -> str:
        temperature = float(getattr(generation_settings, "temperature", 0.2) or 0.2)
        return await asyncio.to_thread(
            self._chat_complete,
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
            temperature,
            800,
        )

    async def generate_json(
        self,
        *,
        prompt: str,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        instruction = (
            "Return strict JSON only. Do not include markdown fences or extra text.\n\n"
            f"{prompt}"
        )
        text = await asyncio.to_thread(
            self._chat_complete,
            [{"role": "user", "content": instruction}],
            temperature,
            1200,
        )
        if not text:
            return {}
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else {"raw": parsed}
        except Exception:
            from .live_preview_pipeline import parse_json_text

            return parse_json_text(text)


class SarvamRecordingTranscriber:
    """Transcribe saved WAV recordings using Sarvam speech-to-text REST API."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.sarvam.ai",
        model: str = "saaras:v3",
        mode: str = "transcribe",
        language_code: str = "unknown",
    ) -> None:
        self._api_key = api_key.strip()
        self._base_url = base_url.rstrip("/")
        self._model = model.strip() or "saaras:v3"
        self._mode = mode.strip() or "transcribe"
        self._language_code = language_code.strip() or "unknown"

    def _multipart_body(self, fields: dict[str, str], file_field: str, filename: str, data: bytes) -> tuple[bytes, str]:
        boundary = f"----sarvam-{uuid.uuid4().hex}"
        lines: list[bytes] = []
        for key, value in fields.items():
            lines.extend(
                [
                    f"--{boundary}\r\n".encode("utf-8"),
                    f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"),
                    f"{value}\r\n".encode("utf-8"),
                ]
            )
        lines.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode("utf-8"),
                b"Content-Type: audio/wav\r\n\r\n",
                data,
                b"\r\n",
                f"--{boundary}--\r\n".encode("utf-8"),
            ]
        )
        return b"".join(lines), boundary

    def transcribe(self, wav_path: Path, language_hint: str | None = None) -> tuple[str, dict[str, str]]:
        if not wav_path.exists():
            return "", {}
        audio_bytes = wav_path.read_bytes()
        language_code = _language_to_bcp47(language_hint, default_code=self._language_code)
        fields = {
            "model": self._model,
            "mode": self._mode,
            "language_code": language_code,
        }
        body, boundary = self._multipart_body(fields, "file", wav_path.name, audio_bytes)
        request = urllib.request.Request(
            url=f"{self._base_url}/speech-to-text",
            data=body,
            method="POST",
            headers={
                "api-subscription-key": self._api_key,
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                raw = response.read().decode("utf-8")
                payload = json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="ignore")
            logger.warning("Sarvam STT HTTP %s for %s: %s", exc.code, wav_path, body_text[:250])
            return "", {}
        except Exception as exc:
            logger.warning("Sarvam STT failed for %s: %s", wav_path, exc)
            return "", {}

        transcript = " ".join(str(payload.get("transcript", "")).split()).strip() if isinstance(payload, dict) else ""
        if not transcript:
            return "", {}
        meta = {
            "mode": "sarvam_rest",
            "model": self._model,
            "language": str(payload.get("language_code", language_code) or language_code),
        }
        return transcript, meta

    async def transcribe_async(self, wav_path: Path, language_hint: str | None = None) -> tuple[str, dict[str, str]]:
        return await asyncio.to_thread(self.transcribe, wav_path, language_hint)
