"""Record caller and agent PCM into a bounded, local stereo WAV file."""
import hashlib
import time
import wave
from uuid import uuid4
import numpy as np


class CallRecording:
    def __init__(self, root, owner, clock=time.monotonic):
        self.root, self.owner, self.clock = root, owner, clock
        self.started = clock()
        self.parts = []
        self.limit = 310 * 24000
        self.samples = 0
        self.positions = [None, None]

    def add(self, pcm, rate, channel):
        if not pcm or self.samples >= self.limit * 2:
            return
        samples = np.frombuffer(pcm, dtype='<i2')
        if rate != 24000:
            size = round(len(samples) * 24000 / rate)
            if not size:
                return
            samples = np.interp(np.arange(size) * rate / 24000, np.arange(len(samples)), samples).astype('<i2')
        arrival = max(0, round((self.clock() - self.started) * 24000))
        # Packet arrival times jitter and can arrive in bursts. Place frames by
        # sample count instead of overlapping and summing neighboring packets.
        previous = self.positions[channel]
        offset = arrival if previous is None or arrival-previous > 4800 else previous
        samples = samples[:max(0, self.limit-offset)].copy()
        if samples.size:
            self.parts.append((offset, channel, samples))
            self.samples += samples.size
            self.positions[channel] = offset + samples.size

    def save(self):
        if not self.parts:
            return None
        length = max(offset + len(samples) for offset, _, samples in self.parts)
        audio = np.zeros((length, 2), dtype=np.int32)
        for offset, channel, samples in self.parts:
            audio[offset:offset+len(samples), channel] += samples
        folder = self.root / '.local/call-history' / hashlib.sha256(self.owner.lower().encode()).hexdigest() / 'recordings'
        folder.mkdir(parents=True, exist_ok=True)
        recording_id = str(uuid4())
        target = folder / (recording_id + '.wav')
        temporary = target.with_suffix('.tmp')
        with wave.open(str(temporary), 'wb') as output:
            output.setnchannels(2)
            output.setsampwidth(2)
            output.setframerate(24000)
            output.writeframes(np.clip(audio, -32768, 32767).astype('<i2').tobytes())
        temporary.replace(target)
        self.parts.clear()
        return recording_id
