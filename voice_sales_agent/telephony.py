"""Telephony provider helpers for outbound calling and bidirectional media bridges."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import fractions
import json
import logging
import math
import os
import re
import socket
import ssl
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import certifi
from fastapi import WebSocket
try:
    import av
    from aiortc import MediaStreamTrack
except Exception:  # pragma: no cover - optional dependency path
    av = None  # type: ignore[assignment]
    MediaStreamTrack = object  # type: ignore[assignment]
try:  # pragma: no cover - optional dependency path
    from piopiy_voice import RestClient as PiopiyRestClient
except Exception:  # pragma: no cover - optional dependency path
    PiopiyRestClient = None  # type: ignore[assignment]

from .config import AppSettings

logger = logging.getLogger(__name__)

TWILIO_SAMPLE_RATE = 8_000
EXOTEL_SAMPLE_RATE = 8_000
EXOTEL_FRAME_MS = 20
PCM16_WIDTH = 2
EXOTEL_FRAME_BYTES = EXOTEL_SAMPLE_RATE * PCM16_WIDTH * EXOTEL_FRAME_MS // 1000
EXOTEL_INPUT_CHUNK_MS = 100
EXOTEL_INPUT_CHUNK_BYTES = 16_000 * PCM16_WIDTH * EXOTEL_INPUT_CHUNK_MS // 1000
EXOTEL_MIN_CHUNK_BYTES = EXOTEL_SAMPLE_RATE * PCM16_WIDTH * EXOTEL_INPUT_CHUNK_MS // 1000
EXOTEL_MIN_CHUNK_MS = EXOTEL_INPUT_CHUNK_MS
EXOTEL_STARTUP_DELAY_SECONDS = 0.0
META_INPUT_CHUNK_BYTES = 16_000 * PCM16_WIDTH * 100 // 1000
AIRTEL_IQ_DEFAULT_HEADERS = {"Content-Type": "application/json"}
AIRTEL_IQ_DEFAULT_REQUEST_TEMPLATE = {
    "to": "{to_number}",
    "from": "{caller_id}",
    "applicationId": "{application_id}",
    "callbackUrl": "{events_callback_url}",
    "statusCallbackUrl": "{status_callback_url}",
    "cdrUrl": "{cdr_callback_url}",
    "mediaUrl": "{ws_url}",
}
META_WHATSAPP_DEFAULT_REQUEST_TEMPLATE = {
    "to": "{to_number}",
    "messaging_product": "whatsapp",
    "action": "connect",
    "session": {
        "sdp_type": "{sdp_type}",
        "sdp": "{sdp}",
    },
    "callback_url": "{status_callback_url}",
    "biz_opaque_callback_data": "{pending_id}",
}


def _build_processing_ambience_cycle(gain: float) -> bytes:
    """Create a subtle telephony-safe click texture for caller-side processing feedback."""
    amplitude = max(0.0, min(gain, 1.0)) * 2400.0
    total_samples = EXOTEL_SAMPLE_RATE
    samples = array("h", [0] * total_samples)
    click_offsets = [0.06, 0.19, 0.37, 0.52, 0.74]
    click_length = int(EXOTEL_SAMPLE_RATE * 0.018)
    for offset in click_offsets:
        start = int(total_samples * offset)
        for index in range(click_length):
            pos = start + index
            if pos >= total_samples:
                break
            envelope = 1.0 - (index / max(click_length, 1))
            polarity = -1.0 if index % 2 else 1.0
            samples[pos] = int(amplitude * envelope * polarity)
    return samples.tobytes()


def _build_mulaw_decode_table() -> list[int]:
    table: list[int] = []
    for value in range(256):
        ulaw = (~value) & 0xFF
        sign = ulaw & 0x80
        exponent = (ulaw >> 4) & 0x07
        mantissa = ulaw & 0x0F
        sample = ((mantissa << 3) + 0x84) << exponent
        sample -= 0x84
        if sign:
            sample = -sample
        table.append(sample)
    return table


MULAW_DECODE_TABLE = _build_mulaw_decode_table()


def _pcm16_to_mulaw(sample: int) -> int:
    sample = max(min(sample, 32767), -32768)
    sign = 0x80 if sample < 0 else 0
    if sample < 0:
        sample = -sample
    sample = min(sample + 0x84, 0x7FFF)

    exponent = 7
    mask = 0x4000
    while exponent > 0 and not (sample & mask):
        exponent -= 1
        mask >>= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa)) & 0xFF


def mulaw_bytes_to_pcm16(payload: bytes) -> bytes:
    samples = array("h", (MULAW_DECODE_TABLE[byte] for byte in payload))
    return samples.tobytes()


def pcm16_to_mulaw_bytes(payload: bytes) -> bytes:
    if not payload:
        return b""
    sample_count = len(payload) // PCM16_WIDTH
    fmt = f"<{sample_count}h"
    samples = struct.unpack(fmt, payload[: sample_count * PCM16_WIDTH])
    return bytes(_pcm16_to_mulaw(sample) for sample in samples)


def _resample_linear(payload: bytes, src_rate: int, dst_rate: int) -> bytes:
    if not payload or src_rate == dst_rate:
        return payload

    source = array("h")
    source.frombytes(payload)
    if not source:
        return b""
    if len(source) == 1:
        return array("h", [source[0]]).tobytes()

    target_length = max(1, math.floor(len(source) * dst_rate / src_rate))
    target = array("h")
    for index in range(target_length):
        position = index * (len(source) - 1) / max(target_length - 1, 1)
        left_index = int(position)
        right_index = min(left_index + 1, len(source) - 1)
        blend = position - left_index
        sample = round(source[left_index] * (1.0 - blend) + source[right_index] * blend)
        target.append(sample)
    return target.tobytes()


def twilio_mulaw_to_pcm16k(payload: bytes) -> bytes:
    pcm_8k = mulaw_bytes_to_pcm16(payload)
    return _resample_linear(pcm_8k, TWILIO_SAMPLE_RATE, 16_000)


def pcm24k_to_twilio_mulaw(payload: bytes) -> bytes:
    pcm_8k = _resample_linear(payload, 24_000, TWILIO_SAMPLE_RATE)
    return pcm16_to_mulaw_bytes(pcm_8k)


def _normalize_pio_dial_number(value: str) -> str:
    """Normalize phone input for Piopiy's digits-only caller validation."""
    return "".join(ch for ch in str(value or "").strip() if ch.isdigit())


class TwilioMediaBridge:
    """Expose a Twilio bidirectional media stream through the audio transport interface."""

    def __init__(self, websocket: WebSocket) -> None:
        self.websocket = websocket
        self.stream_sid: str | None = None
        self.call_sid: str | None = None
        self._incoming_audio: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._send_lock = asyncio.Lock()
        self._playback_idle = asyncio.Event()
        self._playback_idle.set()
        self._playback_active = False
        self._pending_marks: set[str] = set()
        self._closed = False
        self._mark_counter = 0

    def open(self) -> None:
        """The WebSocket is already open when telephony starts."""

    async def handle_ws_message(self, message: dict[str, Any]) -> None:
        event = message.get("event")
        if event == "start":
            start = message.get("start", {})
            self.stream_sid = message.get("streamSid") or start.get("streamSid")
            self.call_sid = start.get("callSid")
            logger.info("Twilio stream started: call_sid=%s stream_sid=%s", self.call_sid, self.stream_sid)
            return
        if event == "media":
            media = message.get("media", {})
            payload = media.get("payload")
            if not payload:
                return
            decoded = base64.b64decode(payload)
            await self._incoming_audio.put(twilio_mulaw_to_pcm16k(decoded))
            return
        if event == "mark":
            mark = message.get("mark", {})
            name = mark.get("name")
            if name in self._pending_marks:
                self._pending_marks.discard(name)
                if not self._pending_marks:
                    self._playback_active = False
                    self._playback_idle.set()
            return
        if event == "clear":
            self._pending_marks.clear()
            self._playback_active = False
            self._playback_idle.set()
            return
        if event == "stop":
            await self._incoming_audio.put(None)
            self._closed = True

    async def mic_chunks(self):
        while True:
            chunk = await self._incoming_audio.get()
            if chunk is None:
                return
            yield chunk

    async def play(self, audio_bytes: bytes) -> None:
        if self._closed or not audio_bytes or self.stream_sid is None:
            return
        payload = pcm24k_to_twilio_mulaw(audio_bytes)
        if not payload:
            return

        self._mark_counter += 1
        mark_name = f"playback-{self._mark_counter}"
        self._pending_marks.add(mark_name)
        self._playback_active = True
        self._playback_idle.clear()

        async with self._send_lock:
            await self.websocket.send_text(
                json.dumps(
                    {
                        "event": "media",
                        "streamSid": self.stream_sid,
                        "media": {"payload": base64.b64encode(payload).decode("ascii")},
                    }
                )
            )
            await self.websocket.send_text(
                json.dumps(
                    {
                        "event": "mark",
                        "streamSid": self.stream_sid,
                        "mark": {"name": mark_name},
                    }
                )
            )

    async def flush_playback(self) -> None:
        if self._closed or self.stream_sid is None:
            return
        self._pending_marks.clear()
        self._playback_active = False
        self._playback_idle.set()
        async with self._send_lock:
            await self.websocket.send_text(
                json.dumps(
                    {
                        "event": "clear",
                        "streamSid": self.stream_sid,
                    }
                )
            )

    def is_playing(self) -> bool:
        return self._playback_active or bool(self._pending_marks)

    def should_drop_input_while_playing(self) -> bool:
        """Telephony input comes from the remote caller, so do not discard it during playback."""
        return False

    async def wait_for_playback_idle(self) -> None:
        await self._playback_idle.wait()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._pending_marks.clear()
        self._playback_active = False
        self._playback_idle.set()
        await self._incoming_audio.put(None)
        try:
            await self.websocket.close()
        except Exception:
            pass


@dataclass(slots=True)
class TwilioPendingCall:
    session_id: str
    client_id: str
    customer_name: str
    to_number: str


@dataclass(slots=True)
class PendingCall:
    session_id: str
    client_id: str
    customer_name: str
    to_number: str
    provider: str
    provider_call_sid: str | None = None
    metadata: dict[str, Any] | None = None


class TwilioCallClient:
    """Create outbound calls through Twilio's REST API."""

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings

    def ensure_configured(self) -> None:
        missing = [
            name
            for name, value in (
                ("PUBLIC_BASE_URL", self.settings.public_base_url),
                ("TWILIO_ACCOUNT_SID", self.settings.twilio_account_sid),
                ("TWILIO_AUTH_TOKEN", self.settings.twilio_auth_token),
                ("TWILIO_FROM_NUMBER", self.settings.twilio_from_number),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"Missing telephony settings: {', '.join(missing)}")
        validate_public_base_url(self.settings.public_base_url or "")

    async def create_call(self, to_number: str, twiml_url: str) -> dict[str, Any]:
        self.ensure_configured()
        return await asyncio.to_thread(self._create_call_sync, to_number, twiml_url)

    def _create_call_sync(self, to_number: str, twiml_url: str) -> dict[str, Any]:
        data = urllib.parse.urlencode(
            {
                "To": to_number,
                "From": self.settings.twilio_from_number or "",
                "Url": twiml_url,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            url=f"https://api.twilio.com/2010-04-01/Accounts/{self.settings.twilio_account_sid}/Calls.json",
            data=data,
            method="POST",
            headers={
                "Authorization": "Basic "
                + base64.b64encode(
                    f"{self.settings.twilio_account_sid}:{self.settings.twilio_auth_token}".encode("utf-8")
                ).decode("ascii"),
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        try:
            with urllib.request.urlopen(request, timeout=30, context=ssl_context) as response:
                payload = response.read().decode("utf-8")
            return json.loads(payload)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Twilio rejected the outbound call request ({exc.code}): {body}") from exc
        except ssl.SSLCertVerificationError as exc:
            raise RuntimeError(
                "Python could not verify Twilio's SSL certificate chain. "
                "The app now uses certifi's CA bundle, so if this still appears, "
                "the local Python SSL environment is intercepting or rewriting certificates."
            ) from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, TimeoutError | socket.timeout):
                raise RuntimeError(
                    "Timed out while connecting to Twilio's API. "
                    "This machine cannot complete the HTTPS handshake to api.twilio.com right now. "
                    "Check your network, VPN, proxy, firewall, or try a different connection."
                ) from exc
            raise RuntimeError(f"Could not connect to Twilio's API: {reason}") from exc


class ExotelMediaBridge:
    """Expose an Exotel bidirectional media stream through the audio transport interface."""

    def __init__(self, websocket: WebSocket, echo_test: bool = False, processing_ambience_gain: float = 0.08) -> None:
        self.websocket = websocket
        self.echo_test = echo_test
        self.stream_sid: str | None = None
        self.call_sid: str | None = None
        self.connected = False
        self._incoming_audio: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._send_lock = asyncio.Lock()
        self._playback_idle = asyncio.Event()
        self._playback_idle.set()
        self._playback_active = False
        self._closed = False
        self._sequence_number = 0
        self._chunk_number = 0
        self._timestamp_ms = 0
        self._outbound_buffer = bytearray()
        self._incoming_buffer = bytearray()
        self._next_send_at = 0.0
        self._processing_ambience_enabled = False
        self._processing_ambience_task: asyncio.Task[None] | None = None
        self._processing_ambience_cycle = _build_processing_ambience_cycle(processing_ambience_gain)
        self._processing_ambience_offset = 0

    def open(self) -> None:
        """The WebSocket is already open when telephony starts."""
        if self._processing_ambience_task is None:
            self._processing_ambience_task = asyncio.create_task(self._processing_ambience_worker())

    async def handle_ws_message(self, message: dict[str, Any]) -> None:
        event = str(message.get("event", "")).strip().lower()
        if event == "connected":
            self.connected = True
            logger.info("Exotel websocket connected event received.")
            return
        if event == "start":
            start = message.get("start", {})
            self.stream_sid = message.get("stream_sid") or start.get("stream_sid")
            self.call_sid = message.get("call_sid") or start.get("call_sid")
            self._next_send_at = time.monotonic() + EXOTEL_STARTUP_DELAY_SECONDS
            logger.info("Exotel stream started: call_sid=%s stream_sid=%s", self.call_sid, self.stream_sid)
            return
        if event == "media":
            media = message.get("media", {})
            payload = media.get("payload")
            if not payload:
                return
            decoded = base64.b64decode(payload)
            if self.echo_test and self.stream_sid is not None and not self._closed:
                try:
                    await self.websocket.send_text(
                        json.dumps(
                            {
                                "event": "media",
                                "stream_sid": self.stream_sid,
                                "media": {"payload": payload},
                            }
                        )
                    )
                    logger.info("Echoed Exotel inbound audio back to caller: stream_sid=%s bytes=%s", self.stream_sid, len(decoded))
                except RuntimeError:
                    self._closed = True
                    return
            self._incoming_buffer.extend(_resample_linear(decoded, 8_000, 16_000))
            while len(self._incoming_buffer) >= EXOTEL_INPUT_CHUNK_BYTES:
                chunk = bytes(self._incoming_buffer[:EXOTEL_INPUT_CHUNK_BYTES])
                del self._incoming_buffer[:EXOTEL_INPUT_CHUNK_BYTES]
                await self._incoming_audio.put(chunk)
            return
        if event == "clear":
            self._incoming_buffer.clear()
            while not self._incoming_audio.empty():
                try:
                    self._incoming_audio.get_nowait()
                except asyncio.QueueEmpty:
                    break
            logger.info("Exotel clear event received: stream_sid=%s", self.stream_sid)
            return
        if event == "stop":
            if self._incoming_buffer:
                await self._incoming_audio.put(bytes(self._incoming_buffer))
                self._incoming_buffer.clear()
            await self._incoming_audio.put(None)
            self._closed = True
            self._playback_active = False
            self._playback_idle.set()

    async def mic_chunks(self):
        while True:
            chunk = await self._incoming_audio.get()
            if chunk is None:
                return
            yield chunk

    async def play(self, audio_bytes: bytes) -> None:
        if self._closed or not audio_bytes or self.stream_sid is None:
            return
        self._processing_ambience_enabled = False
        payload = _resample_linear(audio_bytes, 24_000, 8_000)
        if not payload:
            return
        self._playback_active = True
        self._playback_idle.clear()
        self._outbound_buffer.extend(payload)

        frame_count = 0
        async with self._send_lock:
            while len(self._outbound_buffer) >= EXOTEL_MIN_CHUNK_BYTES:
                frame = bytes(self._outbound_buffer[:EXOTEL_MIN_CHUNK_BYTES])
                del self._outbound_buffer[:EXOTEL_MIN_CHUNK_BYTES]
                now = time.monotonic()
                if self._next_send_at <= 0.0:
                    self._next_send_at = now
                delay = self._next_send_at - now
                if delay > 0:
                    await asyncio.sleep(delay)
                if self._closed:
                    break
                self._sequence_number += 1
                self._chunk_number += 1
                self._timestamp_ms += EXOTEL_MIN_CHUNK_MS
                self._next_send_at = max(self._next_send_at, time.monotonic()) + (EXOTEL_MIN_CHUNK_MS / 1000)
                frame_count += 1
                if frame_count <= 3:
                    logger.info(
                        "Sending Exotel outbound audio: stream_sid=%s sequence=%s chunk=%s timestamp=%s bytes=%s delay_ms=%s startup=%s",
                        self.stream_sid,
                        self._sequence_number,
                        self._chunk_number,
                        self._timestamp_ms,
                        len(frame),
                        max(0, round(delay * 1000)),
                        frame_count == 0,
                    )
                try:
                    await self.websocket.send_text(
                        json.dumps(
                            {
                                "event": "media",
                                "stream_sid": self.stream_sid,
                                "media": {
                                    "payload": base64.b64encode(frame).decode("ascii"),
                                },
                            }
                        )
                    )
                except RuntimeError:
                    self._closed = True
                    break
        self._playback_active = False
        self._playback_idle.set()

    async def flush_playback(self) -> None:
        self._outbound_buffer.clear()
        self._next_send_at = 0.0
        self._playback_active = False
        self._playback_idle.set()

    def is_playing(self) -> bool:
        return self._playback_active

    def should_drop_input_while_playing(self) -> bool:
        return False

    async def wait_for_playback_idle(self) -> None:
        await self._playback_idle.wait()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._processing_ambience_enabled = False
        self._outbound_buffer.clear()
        self._next_send_at = 0.0
        self._playback_active = False
        self._playback_idle.set()
        if self._processing_ambience_task is not None:
            self._processing_ambience_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._processing_ambience_task
            self._processing_ambience_task = None
        await self._incoming_audio.put(None)
        try:
            await self.websocket.close()
        except Exception:
            pass

    async def _send_mark(self, name: str) -> None:
        if self.stream_sid is None or self._closed:
            return
        logger.info("Sending Exotel mark: stream_sid=%s name=%s", self.stream_sid, name)
        await self.websocket.send_text(
            json.dumps(
                {
                    "event": "mark",
                    "stream_sid": self.stream_sid,
                    "mark": {
                        "name": name,
                    },
                }
            )
        )

    async def set_processing_ambience(self, enabled: bool) -> None:
        self._processing_ambience_enabled = enabled and not self._closed

    async def _processing_ambience_worker(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(EXOTEL_MIN_CHUNK_MS / 1000)
                if (
                    not self._processing_ambience_enabled
                    or self.stream_sid is None
                    or self._playback_active
                    or self._closed
                ):
                    continue
                frame = self._next_processing_ambience_frame()
                if not frame:
                    continue
                async with self._send_lock:
                    if self._closed or self.stream_sid is None or self._playback_active or not self._processing_ambience_enabled:
                        continue
                    self._sequence_number += 1
                    self._chunk_number += 1
                    self._timestamp_ms += EXOTEL_MIN_CHUNK_MS
                    try:
                        await self.websocket.send_text(
                            json.dumps(
                                {
                                    "event": "media",
                                    "stream_sid": self.stream_sid,
                                    "media": {
                                        "payload": base64.b64encode(frame).decode("ascii"),
                                    },
                                }
                            )
                        )
                    except RuntimeError:
                        self._closed = True
                        break
        except asyncio.CancelledError:
            return

    def _next_processing_ambience_frame(self) -> bytes:
        if not self._processing_ambience_cycle:
            return b""
        frame = bytearray()
        remaining = EXOTEL_MIN_CHUNK_BYTES
        while remaining > 0:
            available = len(self._processing_ambience_cycle) - self._processing_ambience_offset
            take = min(remaining, available)
            frame.extend(
                self._processing_ambience_cycle[
                    self._processing_ambience_offset : self._processing_ambience_offset + take
                ]
            )
            self._processing_ambience_offset = (self._processing_ambience_offset + take) % len(
                self._processing_ambience_cycle
            )
            remaining -= take
        return bytes(frame)


class ExotelCallClient:
    """Create outbound calls through Exotel's REST API."""

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings

    def ensure_configured(self) -> None:
        missing = [
            name
            for name, value in (
                ("PUBLIC_BASE_URL", self.settings.public_base_url),
                ("EXOTEL_ACCOUNT_SID", self.settings.exotel_account_sid),
                ("EXOTEL_API_KEY", self.settings.exotel_api_key),
                ("EXOTEL_API_TOKEN", self.settings.exotel_api_token),
                ("EXOTEL_CALLER_ID", self.settings.exotel_caller_id),
                ("EXOTEL_SUBDOMAIN", self.settings.exotel_subdomain),
                ("EXOTEL_APP_ID", self.settings.exotel_app_id),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"Missing telephony settings: {', '.join(missing)}")
        validate_public_base_url(self.settings.public_base_url or "")

    async def create_call(self, to_number: str, status_callback_url: str) -> dict[str, Any]:
        self.ensure_configured()
        return await asyncio.to_thread(self._create_call_sync, to_number, status_callback_url)

    def _create_call_sync(self, to_number: str, status_callback_url: str) -> dict[str, Any]:
        exoml_url = f"http://my.exotel.in/exoml/start/{self.settings.exotel_app_id}"
        data = urllib.parse.urlencode(
            {
                "From": to_number,
                "CallerId": self.settings.exotel_caller_id or "",
                "CallType": "trans",
                "Url": exoml_url,
                "StatusCallback": status_callback_url,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            url=(
                f"https://{self.settings.exotel_subdomain}"
                f"/v1/Accounts/{self.settings.exotel_account_sid}/Calls/connect"
            ),
            data=data,
            method="POST",
            headers={
                "Authorization": "Basic "
                + base64.b64encode(
                    f"{self.settings.exotel_api_key}:{self.settings.exotel_api_token}".encode("utf-8")
                ).decode("ascii"),
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        try:
            with urllib.request.urlopen(request, timeout=30, context=ssl_context) as response:
                payload = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Exotel rejected the outbound call request ({exc.code}): {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Could not connect to Exotel's API: {exc.reason}") from exc

        root = ET.fromstring(payload)
        call = root.find(".//Call")
        if call is None:
            raise RuntimeError("Exotel response did not include a Call payload.")
        return {
            "sid": call.findtext("Sid", default=""),
            "status": call.findtext("Status", default="queued"),
            "from": call.findtext("From", default=to_number),
            "to": call.findtext("To", default=self.settings.exotel_caller_id or ""),
        }


def _replace_template_placeholders(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        rendered = value
        for key, replacement in replacements.items():
            rendered = rendered.replace(f"{{{key}}}", replacement)
        return rendered
    if isinstance(value, list):
        return [_replace_template_placeholders(item, replacements) for item in value]
    if isinstance(value, dict):
        return {str(key): _replace_template_placeholders(item, replacements) for key, item in value.items()}
    return value


def _normalize_provider_response(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        sid = (
            payload.get("sid")
            or payload.get("vmSessionId")
            or payload.get("callSid")
            or payload.get("call_id")
            or payload.get("id")
            or payload.get("requestId")
            or ""
        )
        status = (
            payload.get("status")
            or payload.get("callStatus")
            or payload.get("state")
            or payload.get("message")
            or "queued"
        )
        return {
            "sid": str(sid),
            "status": str(status),
            "raw": payload,
        }
    return {"sid": "", "status": "queued", "raw": payload}


class AirtelIQCallClient:
    """Create outbound calls through Airtel IQ using a configurable JSON request template."""

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings

    def ensure_configured(self) -> None:
        missing = [
            name
            for name, value in (
                ("PUBLIC_BASE_URL", self.settings.public_base_url),
                ("AIRTEL_IQ_API_URL or AIRTEL_IQ_BASE_URL", self.settings.airtel_iq_api_url or self.settings.airtel_iq_base_url),
                ("AIRTEL_IQ_API_KEY", self.settings.airtel_iq_api_key),
                ("AIRTEL_IQ_APPLICATION_ID", self.settings.airtel_iq_application_id),
                ("AIRTEL_IQ_CALLER_ID", self.settings.airtel_iq_caller_id),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"Missing telephony settings: {', '.join(missing)}")
        validate_public_base_url(self.settings.public_base_url or "")

    async def create_call(
        self,
        to_number: str,
        status_callback_url: str,
        ws_url: str,
        events_callback_url: str | None = None,
        cdr_callback_url: str | None = None,
    ) -> dict[str, Any]:
        self.ensure_configured()
        return await asyncio.to_thread(
            self._create_call_sync,
            to_number,
            status_callback_url,
            ws_url,
            events_callback_url,
            cdr_callback_url,
        )

    def _resolve_url(self, path: str) -> str:
        if self.settings.airtel_iq_api_url:
            return self.settings.airtel_iq_api_url
        base = (self.settings.airtel_iq_base_url or "").rstrip("/")
        if not base:
            raise RuntimeError("Missing AIRTEL_IQ_BASE_URL or AIRTEL_IQ_API_URL.")
        clean_path = path if path.startswith("/") else f"/{path}"
        return f"{base}{clean_path}"

    def _build_headers(self) -> dict[str, str]:
        headers = dict(AIRTEL_IQ_DEFAULT_HEADERS)
        if self.settings.airtel_iq_headers_json:
            try:
                custom_headers = json.loads(self.settings.airtel_iq_headers_json)
            except json.JSONDecodeError as exc:
                raise RuntimeError("AIRTEL_IQ_HEADERS_JSON must be valid JSON.") from exc
            if not isinstance(custom_headers, dict):
                raise RuntimeError("AIRTEL_IQ_HEADERS_JSON must be a JSON object.")
            headers.update({str(key): str(value) for key, value in custom_headers.items()})

        lowered = {key.lower(): value for key, value in headers.items()}
        if "authorization" not in lowered and "x-api-key" not in lowered:
            headers["x-api-key"] = self.settings.airtel_iq_api_key or ""
        if self.settings.airtel_iq_api_secret and "x-api-secret" not in lowered:
            headers["x-api-secret"] = self.settings.airtel_iq_api_secret
        return headers

    def _post_json_sync(self, url: str, body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        request = urllib.request.Request(
            url=url,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers=headers,
        )
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        try:
            with urllib.request.urlopen(request, timeout=30, context=ssl_context) as response:
                payload = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Airtel IQ request failed ({exc.code}): {body_text}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Could not connect to Airtel IQ's API: {exc.reason}") from exc

        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            parsed = {"raw_response": payload}
        return parsed if isinstance(parsed, dict) else {"raw_response": parsed}

    def _create_call_sync(
        self,
        to_number: str,
        status_callback_url: str,
        ws_url: str,
        events_callback_url: str | None,
        cdr_callback_url: str | None,
    ) -> dict[str, Any]:
        request_template = AIRTEL_IQ_DEFAULT_REQUEST_TEMPLATE
        if self.settings.airtel_iq_request_template_json:
            try:
                request_template = json.loads(self.settings.airtel_iq_request_template_json)
            except json.JSONDecodeError as exc:
                raise RuntimeError("AIRTEL_IQ_REQUEST_TEMPLATE_JSON must be valid JSON.") from exc

        replacements = {
            "to_number": to_number,
            "caller_id": self.settings.airtel_iq_caller_id or "",
            "status_callback_url": status_callback_url,
            "events_callback_url": events_callback_url or status_callback_url,
            "callback_url": events_callback_url or status_callback_url,
            "cdr_callback_url": cdr_callback_url or status_callback_url,
            "cdr_url": cdr_callback_url or status_callback_url,
            "ws_url": ws_url,
            "application_id": self.settings.airtel_iq_application_id or "",
            "api_key": self.settings.airtel_iq_api_key or "",
            "api_secret": self.settings.airtel_iq_api_secret or "",
            "public_base_url": self.settings.public_base_url or "",
        }
        body = _replace_template_placeholders(request_template, replacements)

        headers = self._build_headers()
        parsed = self._post_json_sync(self._resolve_url(self.settings.airtel_iq_initiate_path), body, headers)
        return _normalize_provider_response(parsed)

    async def play_audio(self, vm_session_id: str, audio_url: str) -> dict[str, Any]:
        self.ensure_configured()
        return await asyncio.to_thread(
            self._post_json_sync,
            self._resolve_url(self.settings.airtel_iq_play_audio_path),
            {"vmSessionId": vm_session_id, "audioUrl": audio_url},
            self._build_headers(),
        )

    async def collect_input(
        self,
        vm_session_id: str,
        timeout: int = 5,
        max_digits: int | None = None,
    ) -> dict[str, Any]:
        self.ensure_configured()
        body: dict[str, Any] = {"vmSessionId": vm_session_id, "timeout": timeout}
        if max_digits is not None:
            body["maxDigits"] = max_digits
        return await asyncio.to_thread(
            self._post_json_sync,
            self._resolve_url(self.settings.airtel_iq_collect_input_path),
            body,
            self._build_headers(),
        )

    async def hangup(self, vm_session_id: str) -> dict[str, Any]:
        self.ensure_configured()
        return await asyncio.to_thread(
            self._post_json_sync,
            self._resolve_url(self.settings.airtel_iq_hangup_path),
            {"vmSessionId": vm_session_id},
            self._build_headers(),
        )


class AirtelIQMediaBridge(ExotelMediaBridge):
    """Current Airtel IQ media bridge assumes an Exotel-like PCM websocket event shape."""


@dataclass(slots=True)
class SmartfloBridgeLatencyStats:
    """Track bridge-stage latency for audio converted before Smartflo send."""

    samples: int = 0
    total_queue_ms: float = 0.0
    total_resample_ms: float = 0.0
    total_dsp_ms: float = 0.0
    total_encode_ms: float = 0.0
    total_end_to_end_ms: float = 0.0
    last_end_to_end_ms: float = 0.0
    max_end_to_end_ms: float = 0.0

    def add(
        self,
        *,
        queue_ms: float,
        resample_ms: float,
        dsp_ms: float,
        encode_ms: float,
        end_to_end_ms: float,
    ) -> None:
        self.samples += 1
        self.total_queue_ms += queue_ms
        self.total_resample_ms += resample_ms
        self.total_dsp_ms += dsp_ms
        self.total_encode_ms += encode_ms
        self.total_end_to_end_ms += end_to_end_ms
        self.last_end_to_end_ms = end_to_end_ms
        self.max_end_to_end_ms = max(self.max_end_to_end_ms, end_to_end_ms)

    def snapshot(self) -> dict[str, float]:
        sample_count = max(self.samples, 1)
        return {
            "samples": float(self.samples),
            "avg_queue_ms": self.total_queue_ms / sample_count,
            "avg_resample_ms": self.total_resample_ms / sample_count,
            "avg_dsp_ms": self.total_dsp_ms / sample_count,
            "avg_encode_ms": self.total_encode_ms / sample_count,
            "avg_end_to_end_ms": self.total_end_to_end_ms / sample_count,
            "last_end_to_end_ms": self.last_end_to_end_ms,
            "max_end_to_end_ms": self.max_end_to_end_ms,
        }


class TelephonyAudioConditioner:
    """Narrowband speech conditioner for telephony-grade outbound frames."""

    def __init__(
        self,
        *,
        sample_rate: int = TWILIO_SAMPLE_RATE,
        enable_dsp: bool = True,
        highpass_hz: int = 100,
        lowpass_hz: int = 3400,
        target_rms_dbfs: float = -19.0,
        enable_noise_gate: bool = True,
        noise_gate_threshold: int = 180,
    ) -> None:
        self.sample_rate = max(8_000, int(sample_rate))
        self.enable_dsp = enable_dsp
        self.enable_noise_gate = enable_noise_gate
        self.noise_gate_threshold = max(0, int(noise_gate_threshold))
        self._target_rms = self._dbfs_to_rms(target_rms_dbfs)
        self._gain = 1.0
        self._hp_alpha = self._rc_alpha(cutoff_hz=max(30, highpass_hz), highpass=True)
        self._lp_alpha = self._rc_alpha(cutoff_hz=max(highpass_hz + 100, lowpass_hz), highpass=False)
        self._hp_prev_x = 0.0
        self._hp_prev_y = 0.0
        self._lp_prev_y = 0.0

    @staticmethod
    def _dbfs_to_rms(dbfs: float) -> float:
        full_scale = 32767.0
        return max(120.0, full_scale * (10.0 ** (dbfs / 20.0)))

    def _rc_alpha(self, *, cutoff_hz: int, highpass: bool) -> float:
        dt = 1.0 / float(self.sample_rate)
        rc = 1.0 / (2.0 * math.pi * max(1.0, float(cutoff_hz)))
        if highpass:
            return rc / (rc + dt)
        return dt / (rc + dt)

    def process_pcm16_8k(self, payload: bytes) -> bytes:
        if not payload or not self.enable_dsp:
            return payload
        samples = array("h")
        samples.frombytes(payload)
        if not samples:
            return payload

        # Stage 1: high-pass + low-pass band limiting.
        filtered: list[float] = []
        hp_alpha = self._hp_alpha
        lp_alpha = self._lp_alpha
        hp_prev_x = self._hp_prev_x
        hp_prev_y = self._hp_prev_y
        lp_prev_y = self._lp_prev_y
        for sample in samples:
            x = float(sample)
            hp_y = hp_alpha * (hp_prev_y + x - hp_prev_x)
            hp_prev_x = x
            hp_prev_y = hp_y
            lp_y = lp_prev_y + lp_alpha * (hp_y - lp_prev_y)
            lp_prev_y = lp_y
            filtered.append(lp_y)
        self._hp_prev_x = hp_prev_x
        self._hp_prev_y = hp_prev_y
        self._lp_prev_y = lp_prev_y

        # Stage 2: AGC toward target RMS with smoothed gain.
        rms = math.sqrt(sum(sample * sample for sample in filtered) / max(1, len(filtered)))
        if rms > 1.0:
            desired_gain = self._target_rms / rms
            desired_gain = max(0.25, min(desired_gain, 6.0))
            self._gain = (0.88 * self._gain) + (0.12 * desired_gain)
        current_gain = self._gain

        # Stage 3: optional light noise gate + soft limiter + clamp.
        out = array("h")
        for sample in filtered:
            value = sample * current_gain
            if self.enable_noise_gate and abs(value) < self.noise_gate_threshold:
                value = 0.0
            # Soft limiting curve to avoid hard clipping artifacts.
            limited = math.tanh(value / 32768.0) * 32767.0
            out.append(int(max(-32768, min(32767, round(limited)))))
        return out.tobytes()


@dataclass(slots=True)
class _SmartfloOutboundChunk:
    payload_24k: bytes
    enqueued_at: float


class SmartfloMediaBridge(TwilioMediaBridge):
    """Smartflo media bridge with telephony conditioning and staged latency tracking."""

    def __init__(self, websocket: WebSocket, settings: AppSettings) -> None:
        super().__init__(websocket)
        frame_ms = max(10, min(60, int(settings.smartflo_frame_ms or 20)))
        self._frame_ms = frame_ms
        self._frame_bytes_8k = TWILIO_SAMPLE_RATE * PCM16_WIDTH * frame_ms // 1000
        queue_frames = max(2, int(max(settings.smartflo_max_queue_ms, frame_ms) / frame_ms))
        self._outbound_queue: asyncio.Queue[_SmartfloOutboundChunk] = asyncio.Queue(maxsize=queue_frames)
        self._outbound_worker_task: asyncio.Task[None] = asyncio.create_task(self._outbound_worker())
        self._latency = SmartfloBridgeLatencyStats()
        self._next_send_at = 0.0
        self._last_latency_log_at = 0.0
        self._latency_log_interval_seconds = max(1.0, float(settings.smartflo_latency_log_interval_seconds or 5.0))
        self._conditioner = TelephonyAudioConditioner(
            sample_rate=TWILIO_SAMPLE_RATE,
            enable_dsp=bool(settings.smartflo_enable_dsp),
            highpass_hz=max(30, int(settings.smartflo_highpass_hz or 100)),
            lowpass_hz=max(600, int(settings.smartflo_lowpass_hz or 3400)),
            target_rms_dbfs=float(settings.smartflo_target_rms_dbfs or -19.0),
            enable_noise_gate=bool(settings.smartflo_enable_noise_gate),
            noise_gate_threshold=max(0, int(settings.smartflo_noise_gate_threshold or 180)),
        )

    async def play(self, audio_bytes: bytes) -> None:
        if self._closed or not audio_bytes:
            return
        chunk = _SmartfloOutboundChunk(payload_24k=audio_bytes, enqueued_at=time.monotonic())
        try:
            self._outbound_queue.put_nowait(chunk)
        except asyncio.QueueFull:
            # Drop the oldest queued chunk to keep tail latency bounded.
            with contextlib.suppress(asyncio.QueueEmpty):
                _ = self._outbound_queue.get_nowait()
                self._outbound_queue.task_done()
            with contextlib.suppress(asyncio.QueueFull):
                self._outbound_queue.put_nowait(chunk)

    async def flush_playback(self) -> None:
        while not self._outbound_queue.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                _ = self._outbound_queue.get_nowait()
                self._outbound_queue.task_done()
        await super().flush_playback()

    async def close(self) -> None:
        if self._closed:
            return
        if self._outbound_worker_task is not None:
            self._outbound_worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._outbound_worker_task
        await super().close()

    def latency_snapshot(self) -> dict[str, float]:
        return self._latency.snapshot()

    async def _outbound_worker(self) -> None:
        try:
            while True:
                chunk = await self._outbound_queue.get()
                try:
                    await self._send_chunk(chunk)
                finally:
                    self._outbound_queue.task_done()
        except asyncio.CancelledError:
            return

    async def _send_chunk(self, chunk: _SmartfloOutboundChunk) -> None:
        if self._closed or self.stream_sid is None or not chunk.payload_24k:
            return

        resample_start = time.monotonic()
        pcm_8k = _resample_linear(chunk.payload_24k, 24_000, TWILIO_SAMPLE_RATE)
        resample_ms = (time.monotonic() - resample_start) * 1000.0
        if not pcm_8k:
            return

        send_start = time.monotonic()
        offset = 0
        while offset < len(pcm_8k) and not self._closed and self.stream_sid is not None:
            frame = pcm_8k[offset : offset + self._frame_bytes_8k]
            offset += self._frame_bytes_8k
            if len(frame) < self._frame_bytes_8k:
                frame += b"\x00" * (self._frame_bytes_8k - len(frame))

            dsp_start = time.monotonic()
            conditioned = self._conditioner.process_pcm16_8k(frame)
            dsp_ms = (time.monotonic() - dsp_start) * 1000.0

            encode_start = time.monotonic()
            encoded = pcm16_to_mulaw_bytes(conditioned)
            encode_ms = (time.monotonic() - encode_start) * 1000.0
            if not encoded:
                continue

            now = time.monotonic()
            if self._next_send_at <= 0.0:
                self._next_send_at = now
            sleep_for = self._next_send_at - now
            if sleep_for > 0.0:
                await asyncio.sleep(sleep_for)
            self._next_send_at = max(self._next_send_at, time.monotonic()) + (self._frame_ms / 1000.0)

            async with self._send_lock:
                await self.websocket.send_text(
                    json.dumps(
                        {
                            "event": "media",
                            "streamSid": self.stream_sid,
                            "media": {"payload": base64.b64encode(encoded).decode("ascii")},
                        }
                    )
                )

            end_to_end_ms = (time.monotonic() - chunk.enqueued_at) * 1000.0
            queue_ms = max(0.0, (send_start - chunk.enqueued_at) * 1000.0)
            self._latency.add(
                queue_ms=queue_ms,
                resample_ms=resample_ms,
                dsp_ms=dsp_ms,
                encode_ms=encode_ms,
                end_to_end_ms=end_to_end_ms,
            )
            self._maybe_log_latency()

    def _maybe_log_latency(self) -> None:
        now = time.monotonic()
        if (now - self._last_latency_log_at) < self._latency_log_interval_seconds:
            return
        self._last_latency_log_at = now
        snapshot = self._latency.snapshot()
        logger.info(
            "Smartflo bridge latency: samples=%s avg_end_to_end_ms=%.2f avg_queue_ms=%.2f avg_resample_ms=%.2f avg_dsp_ms=%.2f avg_encode_ms=%.2f max_end_to_end_ms=%.2f",
            int(snapshot["samples"]),
            snapshot["avg_end_to_end_ms"],
            snapshot["avg_queue_ms"],
            snapshot["avg_resample_ms"],
            snapshot["avg_dsp_ms"],
            snapshot["avg_encode_ms"],
            snapshot["max_end_to_end_ms"],
        )


class PiopiyCallClient:
    """Create outbound calls through Piopiy's AI call API."""

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings

    def ensure_configured(
        self,
        *,
        agent_id: str | None = None,
        caller_id: str | None = None,
        app_id: str | None = None,
    ) -> None:
        resolved_caller_id = (caller_id or self.settings.piopiy_caller_id or "").strip()
        resolved_app_id = (app_id or self.settings.piopiy_app_id or "").strip()
        missing = [
            name
            for name, value in (
                ("PIOPIY_API_TOKEN", self.settings.piopiy_api_token),
                ("PIOPIY_CALLER_ID", resolved_caller_id),
                ("PIOPIY_APP_ID", resolved_app_id),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"Missing telephony settings: {', '.join(missing)}")
        if PiopiyRestClient is None:
            raise RuntimeError(
                "Piopiy support is not installed in this environment. Install the `piopiy` package first."
            )

    async def create_call(
        self,
        *,
        to_number: str,
        agent_id: str | None = None,
        caller_id: str | None = None,
        app_id: str | None = None,
    ) -> dict[str, Any]:
        self.ensure_configured(agent_id=agent_id, caller_id=caller_id, app_id=app_id)
        return await asyncio.to_thread(
            self._create_call_sync,
            to_number,
            agent_id,
            caller_id,
            app_id,
        )

    def _create_call_sync(
        self,
        to_number: str,
        agent_id: str | None,
        caller_id: str | None,
        app_id: str | None,
    ) -> dict[str, Any]:
        client = PiopiyRestClient(token=self.settings.piopiy_api_token)
        resolved_caller_id = (caller_id or self.settings.piopiy_caller_id or "").strip()
        resolved_app_id = (app_id or self.settings.piopiy_app_id or "").strip()
        normalized_to_number = _normalize_pio_dial_number(to_number)
        if not re.fullmatch(r"[1-9][0-9]{6,15}", normalized_to_number):
            raise RuntimeError(
                "Piopiy requires a digits-only destination number with 7 to 16 digits, for example 919876543210."
            )
        response = client.voice.call(
            caller_id=resolved_caller_id,
            to_number=normalized_to_number,
            app_id=resolved_app_id,
        )
        normalized = response if isinstance(response, dict) else {"raw_response": response}
        sid = str(
            normalized.get("sid")
            or normalized.get("call_sid")
            or normalized.get("callSid")
            or normalized.get("id")
            or normalized.get("call_id")
            or ""
        ).strip()
        status = str(normalized.get("status") or normalized.get("state") or "queued").strip() or "queued"
        normalized["sid"] = sid
        normalized["status"] = status
        return normalized


class PiopiyMediaBridge:
    """Expose Piopiy binary stream audio through the session audio transport interface."""

    def __init__(self, websocket: WebSocket) -> None:
        self.websocket = websocket
        self._incoming_audio: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._send_lock = asyncio.Lock()
        self._closed = False
        self.stream_sid: str | None = None
        self.call_sid: str | None = None

    def open(self) -> None:
        """The websocket is already open when Piopiy streaming begins."""

    async def handle_ws_message(self, message: dict[str, Any]) -> None:
        event = str(message.get("event") or message.get("status") or "").strip().lower()
        if event in {"start", "connected", "stream_connected"}:
            self.stream_sid = str(message.get("cmiuuid") or message.get("callSid") or "").strip() or self.stream_sid
            self.call_sid = str(message.get("callSid") or message.get("cmiuuid") or "").strip() or self.call_sid
            return
        if event in {"stop", "hangup", "stream_disconnected", "stream_error"}:
            await self._incoming_audio.put(None)
            self._closed = True

    async def push_binary(self, payload: bytes) -> None:
        if self._closed or not payload:
            return
        await self._incoming_audio.put(payload)

    async def mic_chunks(self):
        while True:
            chunk = await self._incoming_audio.get()
            if chunk is None:
                return
            yield chunk

    async def play(self, audio_bytes: bytes) -> None:
        if self._closed or not audio_bytes:
            return
        async with self._send_lock:
            try:
                await self.websocket.send_bytes(audio_bytes)
            except Exception:
                logger.debug("Piopiy outbound audio send failed.", exc_info=True)

    async def flush_playback(self) -> None:
        # Piopiy does not require explicit clear frames for the current bridge path.
        return

    def is_playing(self) -> bool:
        return False

    def should_drop_input_while_playing(self) -> bool:
        return False

    async def wait_for_playback_idle(self) -> None:
        return

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._incoming_audio.put(None)
        try:
            await self.websocket.close()
        except Exception:
            pass


class _MetaOutgoingAudioTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._pts = 0
        self._samples_per_frame = 960  # 20ms @ 48kHz
        self._sample_rate = 48_000
        self._sent_frames = 0

    async def push_pcm48k(self, payload: bytes) -> None:
        if payload:
            await self._queue.put(payload)

    async def close_track(self) -> None:
        await self._queue.put(None)

    async def recv(self):  # type: ignore[override]
        if av is None:
            raise RuntimeError("PyAV is required for Meta WhatsApp media track.")
        chunk = await self._queue.get()
        if chunk is None:
            raise asyncio.CancelledError
        needed_bytes = self._samples_per_frame * PCM16_WIDTH
        if len(chunk) < needed_bytes:
            chunk = chunk + b"\x00" * (needed_bytes - len(chunk))
        elif len(chunk) > needed_bytes:
            chunk = chunk[:needed_bytes]
        frame = av.AudioFrame(format="s16", layout="mono", samples=self._samples_per_frame)
        frame.sample_rate = self._sample_rate
        frame.time_base = fractions.Fraction(1, self._sample_rate)
        frame.pts = self._pts
        self._pts += self._samples_per_frame
        frame.planes[0].update(chunk)
        self._sent_frames += 1
        if self._sent_frames <= 5:
            logger.info("Meta outgoing audio frame sent: frames=%s bytes=%s", self._sent_frames, len(chunk))
        return frame


class MetaWhatsAppMediaBridge:
    """Bridge aiortc audio tracks to the same transport contract used by VoiceSalesSession."""

    def __init__(self, outbound_source_rate: int = 24_000, outbound_preroll_ms: int = 140) -> None:
        self._incoming_audio: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._outgoing_track = _MetaOutgoingAudioTrack()
        self._playback_idle = asyncio.Event()
        self._playback_idle.set()
        self._playback_active = False
        self._closed = False
        self._receiver_tasks: set[asyncio.Task[None]] = set()
        self._playback_clear_task: asyncio.Task[None] | None = None
        self._play_chunks = 0
        self._recv_chunks = 0
        self._incoming_buffer = bytearray()
        self._outbound_source_rate = max(8_000, int(outbound_source_rate or 24_000))
        self._outbound_preroll_ms = max(0, int(outbound_preroll_ms or 0))
        self._sent_first_audio = False

    def open(self) -> None:
        return

    def bind_peer_connection(self, peer_connection: Any) -> None:
        @peer_connection.on("track")
        def on_track(track: Any) -> None:
            if getattr(track, "kind", "") != "audio":
                return
            task = asyncio.create_task(self._consume_remote_track(track))
            self._receiver_tasks.add(task)
            task.add_done_callback(lambda t: self._receiver_tasks.discard(t))

    async def _consume_remote_track(self, track: Any) -> None:
        while not self._closed:
            try:
                frame = await track.recv()
            except Exception:
                break
            pcm_frame = frame
            if hasattr(frame, "reformat"):
                pcm_frame = frame.reformat(format="s16", layout="mono")
            frame_bytes = bytes(pcm_frame.planes[0]) if pcm_frame.planes else b""
            source_rate = int(getattr(pcm_frame, "sample_rate", 16_000) or 16_000)
            source_channels = 1
            layout = getattr(pcm_frame, "layout", None)
            if layout is not None and hasattr(layout, "channels"):
                try:
                    source_channels = max(1, len(layout.channels))
                except Exception:
                    source_channels = 1
            sample_count = int(getattr(pcm_frame, "samples", 0) or 0)
            expected_bytes = sample_count * PCM16_WIDTH * source_channels
            if expected_bytes > 0 and len(frame_bytes) > expected_bytes:
                # Guard against padded plane buffers; keep only true audio payload.
                frame_bytes = frame_bytes[:expected_bytes]
            if source_channels > 1 and len(frame_bytes) >= source_channels * PCM16_WIDTH:
                interleaved = array("h")
                interleaved.frombytes(frame_bytes)
                mono = array("h")
                for index in range(0, len(interleaved) - source_channels + 1, source_channels):
                    window = interleaved[index : index + source_channels]
                    mono.append(round(sum(window) / source_channels))
                frame_bytes = mono.tobytes()
            if source_rate != 16_000:
                frame_bytes = _resample_linear(frame_bytes, source_rate, 16_000)
            if frame_bytes:
                self._recv_chunks += 1
                if self._recv_chunks <= 5:
                    logger.info(
                        "Meta inbound audio chunk received: chunks=%s bytes=%s src_rate=%s src_channels=%s samples=%s",
                        self._recv_chunks,
                        len(frame_bytes),
                        source_rate,
                        source_channels,
                        sample_count,
                    )
                self._incoming_buffer.extend(frame_bytes)
                while len(self._incoming_buffer) >= META_INPUT_CHUNK_BYTES:
                    chunk = bytes(self._incoming_buffer[:META_INPUT_CHUNK_BYTES])
                    del self._incoming_buffer[:META_INPUT_CHUNK_BYTES]
                    await self._incoming_audio.put(chunk)

    async def mic_chunks(self):
        while True:
            chunk = await self._incoming_audio.get()
            if chunk is None:
                return
            yield chunk

    async def play(self, audio_bytes: bytes) -> None:
        if self._closed or not audio_bytes:
            return
        payload = _resample_linear(audio_bytes, self._outbound_source_rate, 48_000)
        if not payload:
            return
        frame_bytes = self._outgoing_track._samples_per_frame * PCM16_WIDTH
        self._playback_active = True
        self._playback_idle.clear()
        total_frames = 0
        try:
            if not self._sent_first_audio and self._outbound_preroll_ms > 0:
                # Warm the RTP path with silence first so dropped startup packets
                # do not eat the opening words from Gemini.
                preroll_samples = int(48_000 * (self._outbound_preroll_ms / 1000.0))
                silence = b"\x00" * (preroll_samples * PCM16_WIDTH)
                silence_offset = 0
                while silence_offset < len(silence):
                    await self._outgoing_track.push_pcm48k(silence[silence_offset : silence_offset + frame_bytes])
                    silence_offset += frame_bytes
                    total_frames += 1
                await asyncio.sleep(min(0.2, self._outbound_preroll_ms / 1000.0))
                self._sent_first_audio = True

            offset = 0
            while offset < len(payload):
                await self._outgoing_track.push_pcm48k(payload[offset : offset + frame_bytes])
                offset += frame_bytes
                total_frames += 1
                self._play_chunks += 1
                if self._play_chunks <= 5:
                    logger.info("Meta outbound audio queued: chunks=%s frame_bytes=%s", self._play_chunks, frame_bytes)
        finally:
            if self._playback_clear_task is not None:
                self._playback_clear_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._playback_clear_task
                self._playback_clear_task = None
            hold_seconds = max(0.1, total_frames * 0.02)
            self._playback_clear_task = asyncio.create_task(self._clear_playback_after(hold_seconds))

    async def _clear_playback_after(self, seconds: float) -> None:
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            return
        self._playback_active = False
        self._playback_idle.set()

    async def flush_playback(self) -> None:
        self._playback_active = False
        self._playback_idle.set()

    def is_playing(self) -> bool:
        return self._playback_active

    def should_drop_input_while_playing(self) -> bool:
        # On WhatsApp media we see strong loopback of agent speech on inbound
        # track; drop inbound while agent playback is active.
        return True

    async def wait_for_playback_idle(self) -> None:
        await self._playback_idle.wait()

    async def set_processing_ambience(self, enabled: bool) -> None:
        return

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._playback_clear_task is not None:
            self._playback_clear_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._playback_clear_task
            self._playback_clear_task = None
        for task in list(self._receiver_tasks):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._receiver_tasks.clear()
        if self._incoming_buffer:
            await self._incoming_audio.put(bytes(self._incoming_buffer))
            self._incoming_buffer.clear()
        await self._incoming_audio.put(None)
        await self._outgoing_track.close_track()

    @property
    def outgoing_track(self) -> _MetaOutgoingAudioTrack:
        return self._outgoing_track


class MetaWhatsAppCallClient:
    """Create outbound WhatsApp calls through Meta's configurable API endpoint."""

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings

    def ensure_configured(self) -> None:
        missing = [
            name
            for name, value in (
                ("PUBLIC_BASE_URL", self.settings.public_base_url),
                ("META_WHATSAPP_BASE_URL", self.settings.meta_whatsapp_base_url),
                ("META_WHATSAPP_ACCESS_TOKEN", self.settings.meta_whatsapp_access_token),
                ("META_WHATSAPP_PHONE_NUMBER_ID", self.settings.meta_whatsapp_phone_number_id),
                ("META_WHATSAPP_FROM_NUMBER", self.settings.meta_whatsapp_from_number),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"Missing telephony settings: {', '.join(missing)}")
        validate_public_base_url(self.settings.public_base_url or "")

    async def create_call(
        self,
        *,
        to_number: str,
        status_callback_url: str,
        pending_id: str,
        client_id: str,
        project_id: str | None,
        sdp_type: str,
        sdp: str,
    ) -> dict[str, Any]:
        self.ensure_configured()
        return await asyncio.to_thread(
            self._create_call_sync,
            to_number,
            status_callback_url,
            pending_id,
            client_id,
            project_id,
            sdp_type,
            sdp,
        )

    def _resolve_url(self, path: str) -> str:
        base = (self.settings.meta_whatsapp_base_url or "").rstrip("/")
        if not base:
            raise RuntimeError("Missing META_WHATSAPP_BASE_URL.")
        templated_path = (
            path.replace("{api_version}", self.settings.meta_whatsapp_api_version)
            .replace("{phone_number_id}", self.settings.meta_whatsapp_phone_number_id or "")
            .replace("{from_number}", self.settings.meta_whatsapp_from_number or "")
        )
        clean_path = templated_path if templated_path.startswith("/") else f"/{templated_path}"
        return f"{base}{clean_path}"

    def _build_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.meta_whatsapp_access_token}",
            "Content-Type": "application/json",
        }

    def _post_json_sync(self, url: str, body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        request = urllib.request.Request(
            url=url,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers=headers,
        )
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        try:
            with urllib.request.urlopen(request, timeout=30, context=ssl_context) as response:
                payload = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Meta WhatsApp call request failed ({exc.code}): {body_text}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Could not connect to Meta WhatsApp API: {exc.reason}") from exc

        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            parsed = {"raw_response": payload}
        return parsed if isinstance(parsed, dict) else {"raw_response": parsed}

    def _create_call_sync(
        self,
        to_number: str,
        status_callback_url: str,
        pending_id: str,
        client_id: str,
        project_id: str | None,
        sdp_type: str,
        sdp: str,
    ) -> dict[str, Any]:
        request_template = META_WHATSAPP_DEFAULT_REQUEST_TEMPLATE
        if self.settings.meta_whatsapp_request_template_json:
            try:
                request_template = json.loads(self.settings.meta_whatsapp_request_template_json)
            except json.JSONDecodeError as exc:
                raise RuntimeError("META_WHATSAPP_REQUEST_TEMPLATE_JSON must be valid JSON.") from exc
        replacements = {
            "to_number": to_number,
            "from_number": self.settings.meta_whatsapp_from_number or "",
            "status_callback_url": status_callback_url,
            "callback_url": status_callback_url,
            "pending_id": pending_id,
            "client_id": client_id,
            "project_id": project_id or "",
            "sdp_type": sdp_type,
            "sdp": sdp,
            "phone_number_id": self.settings.meta_whatsapp_phone_number_id or "",
            "api_version": self.settings.meta_whatsapp_api_version,
            "public_base_url": self.settings.public_base_url or "",
        }
        body = _replace_template_placeholders(request_template, replacements)
        parsed = self._post_json_sync(
            self._resolve_url(self.settings.meta_whatsapp_initiate_path),
            body,
            self._build_headers(),
        )
        return _normalize_provider_response(parsed)

    async def perform_call_action(self, body: dict[str, Any]) -> dict[str, Any]:
        self.ensure_configured()
        return await asyncio.to_thread(
            self._post_json_sync,
            self._resolve_url(self.settings.meta_whatsapp_initiate_path),
            body,
            self._build_headers(),
        )

    async def send_text_message(self, to_number: str, body_text: str) -> dict[str, Any]:
        self.ensure_configured()
        return await asyncio.to_thread(self._send_text_message_sync, to_number, body_text)

    def _send_text_message_sync(self, to_number: str, body_text: str) -> dict[str, Any]:
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_number,
            "type": "text",
            "text": {"body": body_text},
        }
        parsed = self._post_json_sync(
            self._resolve_url(self.settings.meta_whatsapp_messages_path),
            payload,
            self._build_headers(),
        )
        return _normalize_provider_response(parsed)


def build_ws_url(public_base_url: str, path: str) -> str:
    parsed = validate_public_base_url(public_base_url)
    base_path = parsed.path.rstrip("/")
    joined_path = f"{base_path}{path}" if base_path else path
    return urllib.parse.urlunparse(("wss", parsed.netloc, joined_path, "", "", ""))


def validate_public_base_url(public_base_url: str) -> urllib.parse.ParseResult:
    parsed = urllib.parse.urlparse(public_base_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise RuntimeError(
            "PUBLIC_BASE_URL must be a public HTTPS URL reachable by your telephony provider, "
            "for example https://your-subdomain.trycloudflare.com"
        )
    return parsed
