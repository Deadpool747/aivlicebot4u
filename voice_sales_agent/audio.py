"""Local microphone capture and speaker playback helpers."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from array import array
from collections.abc import AsyncIterator
from time import monotonic

import pyaudio

from .constants import (
    CHANNELS,
    INPUT_CHUNK_SIZE,
    MIC_ENERGY_THRESHOLD,
    MIC_HANGOVER_CHUNKS,
    INPUT_SAMPLE_RATE,
    OUTPUT_FRAMES_PER_BUFFER,
    OUTPUT_SAMPLE_RATE,
)

logger = logging.getLogger(__name__)


class LocalAudioIO:
    """Wrap microphone capture and speaker playback for the CLI demo."""

    def __init__(self) -> None:
        self._pa = pyaudio.PyAudio()
        self._input_stream = None
        self._output_stream = None
        self._playback_lock = asyncio.Lock()
        self._playback_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._playback_task: asyncio.Task[None] | None = None
        self._playback_idle = asyncio.Event()
        self._playback_idle.set()
        self._playback_active = False
        self._playback_guard_until = 0.0

    def open(self) -> None:
        """Open microphone and speaker streams."""
        self._input_stream = self._pa.open(
            format=pyaudio.paInt16,
            channels=CHANNELS,
            rate=INPUT_SAMPLE_RATE,
            input=True,
            frames_per_buffer=INPUT_CHUNK_SIZE,
        )
        self._output_stream = self._pa.open(
            format=pyaudio.paInt16,
            channels=CHANNELS,
            rate=OUTPUT_SAMPLE_RATE,
            output=True,
            frames_per_buffer=OUTPUT_FRAMES_PER_BUFFER,
        )
        if self._playback_task is None:
            self._playback_task = asyncio.create_task(self._playback_worker())

    async def mic_chunks(self) -> AsyncIterator[bytes]:
        """Yield near-real-time microphone audio chunks."""
        # TODO: Replace this local microphone transport with a telephony/WebRTC transport
        # once remote calling channels are added. The rest of the app should stay unchanged.
        if self._input_stream is None:
            raise RuntimeError("Input stream is not open.")

        voiced_hangover = 0
        while True:
            chunk = await asyncio.to_thread(
                self._input_stream.read,
                INPUT_CHUNK_SIZE,
                exception_on_overflow=False,
            )
            if MIC_ENERGY_THRESHOLD <= 0:
                yield chunk
                continue
            if self._chunk_energy(chunk) >= MIC_ENERGY_THRESHOLD:
                voiced_hangover = MIC_HANGOVER_CHUNKS
                yield chunk
                continue
            if voiced_hangover > 0:
                voiced_hangover -= 1
                yield chunk

    @staticmethod
    def _chunk_energy(chunk: bytes) -> float:
        """Return a simple average absolute amplitude for PCM16 mono audio."""
        samples = array("h")
        samples.frombytes(chunk)
        if not samples:
            return 0.0
        return sum(abs(sample) for sample in samples) / len(samples)

    async def play(self, audio_bytes: bytes) -> None:
        """Play model audio on the local speaker."""
        if self._output_stream is None:
            raise RuntimeError("Output stream is not open.")
        if audio_bytes:
            self._playback_idle.clear()
            await self._playback_queue.put(audio_bytes)

    async def _playback_worker(self) -> None:
        """Write queued audio chunks continuously to reduce audible gaps."""
        while True:
            chunk = await self._playback_queue.get()
            if chunk is None:
                self._playback_active = False
                self._playback_idle.set()
                return

            chunks = [chunk]
            while True:
                try:
                    next_chunk = self._playback_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if next_chunk is None:
                    self._playback_active = False
                    self._playback_idle.set()
                    return
                chunks.append(next_chunk)

            if self._output_stream is None:
                continue
            async with self._playback_lock:
                self._playback_active = True
                await asyncio.to_thread(self._output_stream.write, b"".join(chunks))
                self._playback_active = False
                self._playback_guard_until = monotonic() + 0.45
                if self._playback_queue.empty():
                    self._playback_idle.set()

    async def flush_playback(self) -> None:
        """Stop current speaker buffer to reduce overlap during barge-in."""
        if self._output_stream is not None:
            while not self._playback_queue.empty():
                try:
                    self._playback_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            try:
                is_active = await asyncio.to_thread(self._output_stream.is_active)
                is_stopped = await asyncio.to_thread(self._output_stream.is_stopped)
                if is_active:
                    await asyncio.to_thread(self._output_stream.stop_stream)
                if is_stopped:
                    await asyncio.to_thread(self._output_stream.start_stream)
                self._playback_active = False
                self._playback_guard_until = 0.0
                self._playback_idle.set()
            except OSError as exc:
                logger.warning("Playback flush encountered a closed stream; reopening output stream. %s", exc)
                await self._reopen_output_stream()

    async def _reopen_output_stream(self) -> None:
        """Recreate the output stream if PyAudio invalidates it."""
        if self._output_stream is not None:
            try:
                await asyncio.to_thread(self._output_stream.close)
            except Exception:
                pass
        self._output_stream = self._pa.open(
            format=pyaudio.paInt16,
            channels=CHANNELS,
            rate=OUTPUT_SAMPLE_RATE,
            output=True,
            frames_per_buffer=OUTPUT_FRAMES_PER_BUFFER,
        )

    def is_playing(self) -> bool:
        """Return whether speaker playback is active or in a short echo-guard window."""
        if self._playback_active:
            return True
        if not self._playback_queue.empty():
            return True
        return monotonic() < self._playback_guard_until

    def should_drop_input_while_playing(self) -> bool:
        """Local mic input should be gated during playback to reduce echo."""
        return True

    async def wait_for_playback_idle(self) -> None:
        """Wait until queued speaker playback is fully finished."""
        await self._playback_idle.wait()

    async def set_processing_ambience(self, enabled: bool) -> None:
        """Local playback does not inject telephony ambience."""
        return

    async def close(self) -> None:
        """Close all audio resources."""
        try:
            if self._playback_task is not None:
                await self._playback_queue.put(None)
                with contextlib.suppress(asyncio.CancelledError):
                    await self._playback_task
                self._playback_task = None
            if self._input_stream is not None:
                self._input_stream.stop_stream()
                self._input_stream.close()
            if self._output_stream is not None:
                self._output_stream.stop_stream()
                self._output_stream.close()
        finally:
            self._playback_active = False
            self._playback_guard_until = 0.0
            self._playback_idle.set()
            self._pa.terminate()
            logger.debug("Audio resources closed.")
