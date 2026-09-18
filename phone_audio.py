"""Suppress quiet telephone background noise while preserving speech tails."""
import numpy as np


class InputNoiseGate:
    def __init__(self, rate=16000, threshold=0.012, release_ms=240):
        self.rate = rate
        self.threshold = threshold
        self.release = rate * release_ms // 1000
        self.remaining = 0
        self.previous_input = 0.0
        self.previous_output = 0.0

    def process(self, pcm):
        samples = np.frombuffer(pcm, dtype='<i2').astype(np.float64) / 32768
        if not len(samples):
            return pcm
        # High pass at about 120 Hz reduces wind/air rumble before level detection.
        alpha = np.exp(-2 * np.pi * 120 / self.rate)
        filtered = np.empty_like(samples)
        for i, sample in enumerate(samples):
            value = alpha * (self.previous_output + sample - self.previous_input)
            self.previous_input, self.previous_output = sample, value
            filtered[i] = value
        rms = float(np.sqrt(np.mean(filtered * filtered)))
        if rms >= self.threshold:
            self.remaining = self.release
        elif self.remaining > 0:
            self.remaining = max(0, self.remaining - len(samples))
        else:
            return bytes(len(pcm))
        return (np.clip(filtered, -1, 1) * 32767).astype('<i2').tobytes()
