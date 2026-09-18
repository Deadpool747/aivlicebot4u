import unittest
import numpy as np
from phone_audio import InputNoiseGate


def tone(level, frequency=500, length=640):
    return (np.sin(np.arange(length) * 2 * np.pi * frequency / 16000) * level * 32767).astype('<i2').tobytes()


class NoiseTests(unittest.TestCase):
    def test_quiet_background_is_silenced(self):
        gate = InputNoiseGate()
        self.assertEqual(gate.process(tone(.003)), bytes(1280))

    def test_speech_passes_and_quiet_tail_is_preserved(self):
        gate = InputNoiseGate()
        self.assertNotEqual(gate.process(tone(.1)), bytes(1280))
        self.assertNotEqual(gate.process(tone(.003)), bytes(1280))
        for _ in range(10):
            output = gate.process(tone(.003))
        self.assertEqual(output, bytes(1280))

    def test_air_rumble_is_suppressed(self):
        gate = InputNoiseGate()
        for _ in range(10):
            output = gate.process(tone(.02, 25))
        self.assertEqual(output, bytes(1280))
