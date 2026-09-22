import asyncio
import base64
import unittest
import numpy as np
from airtel_playback import AirtelPlayback, Goodbye, SpeechActivity


class Socket:
    def __init__(self):
        self.messages = []

    async def send_json(self, message):
        self.messages.append(message)


class PlaybackTests(unittest.IsolatedAsyncioTestCase):
    def test_conversion_is_independent_of_chunk_boundaries(self):
        pcm = (np.sin(np.arange(4800) * 0.13) * 12000).astype('<i2').tobytes()
        whole = AirtelPlayback(Socket(), 'test').encode(pcm)
        split = AirtelPlayback(Socket(), 'test')
        self.assertEqual(whole, b''.join(split.encode(pcm[i:i+514]) for i in range(0, len(pcm), 514)))

    async def test_complete_frames_and_stale_audio_dropped(self):
        socket = Socket()
        player = AirtelPlayback(socket, 'test')
        await player.play(bytes(100), 0)
        self.assertFalse(socket.messages)
        await player.play(bytes(1820), 0)
        self.assertEqual([len(base64.b64decode(m['media']['payload'])) for m in socket.messages], [160, 160])
        await player.clear()
        await player.play(bytes(960), 0)
        self.assertEqual(socket.messages[-1]['event'], 'clear')

    async def test_hangup_waits_for_turn_queue_and_mark(self):
        socket = Socket()
        player = AirtelPlayback(socket, 'test')
        queue, ended = asyncio.Queue(), asyncio.Event()
        goodbye = Goodbye(player, queue, socket, ended, grace=0)
        goodbye.requested = True
        await queue.put(b'final audio')
        self.assertIsNone(goodbye.task)
        goodbye.turn_complete()
        await asyncio.sleep(0)
        self.assertFalse(socket.messages)
        queue.get_nowait()
        queue.task_done()
        await asyncio.sleep(0)
        self.assertEqual(socket.messages[-1]['event'], 'mark')
        self.assertFalse(goodbye.sent)
        player.acknowledge(socket.messages[-1])
        for _ in range(6):
            await asyncio.sleep(0)
        self.assertTrue(goodbye.sent)
        self.assertEqual(socket.messages[-1]['event'], 'terminate')
        ended.set()
        await goodbye.task

    async def test_interruption_cancels_goodbye_even_if_mark_echoed(self):
        socket = Socket()
        player = AirtelPlayback(socket, 'test')
        goodbye = Goodbye(player, asyncio.Queue(), socket, asyncio.Event(), grace=0)
        goodbye.requested = True
        goodbye.turn_complete()
        task = goodbye.task
        await asyncio.sleep(0)
        mark = socket.messages[-1]
        goodbye.interrupt()
        await player.clear()
        player.acknowledge(mark)
        await asyncio.gather(task, return_exceptions=True)
        self.assertFalse(any(m['event'] == 'terminate' for m in socket.messages))

    async def test_speech_during_grace_cancels_before_transcript(self):
        socket = Socket()
        player = AirtelPlayback(socket, 'test')
        goodbye = Goodbye(player, asyncio.Queue(), socket, asyncio.Event(), grace=0.02)
        goodbye.request()
        goodbye.turn_complete()
        task = goodbye.task
        await asyncio.sleep(0)
        player.acknowledge(socket.messages[-1])
        await asyncio.sleep(0.005)
        activity = SpeechActivity()
        speech = (np.ones(320) * 1000).astype('<i2').tobytes()
        for _ in range(3):
            if activity.process(speech):
                goodbye.interrupt()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(goodbye.state, 'NORMAL_CONVERSATION')
        self.assertFalse(goodbye.sent)

    async def test_termination_cannot_be_reopened_by_late_transcription(self):
        socket = Socket()
        player = AirtelPlayback(socket, 'test')
        goodbye = Goodbye(player, asyncio.Queue(), socket, asyncio.Event(), grace=0)
        goodbye.request()
        goodbye.turn_complete()
        task = goodbye.task
        await asyncio.sleep(0)
        player.acknowledge(socket.messages[-1])
        for _ in range(6):
            await asyncio.sleep(0)
        goodbye.interrupt()
        self.assertIs(goodbye.task, task)
        self.assertFalse(goodbye.request())
        goodbye.stopped()
        await task
        self.assertTrue(goodbye.confirmed)
        self.assertEqual(sum(m['event'] == 'terminate' for m in socket.messages), 1)

    def test_continuation_rejects_stale_close_and_later_ending_allowed(self):
        goodbye = Goodbye(None, None, None, None)
        goodbye.request()
        goodbye.interrupt()
        goodbye.allow_close = False
        self.assertFalse(goodbye.request())
        self.assertEqual(goodbye.state, 'NORMAL_CONVERSATION')
        goodbye.allow_close = True
        self.assertTrue(goodbye.request())

    def test_weak_noise_does_not_trigger_speech(self):
        activity = SpeechActivity()
        for _ in range(100):
            self.assertFalse(activity.process((np.ones(320) * 90).astype('<i2').tobytes()))
        self.assertFalse(activity.active)

    async def test_long_and_consecutive_audio_preserves_samples(self):
        socket = Socket()
        player = AirtelPlayback(socket, 'test')
        async def no_wait(frame, version):
            if version == player.version:
                await socket.send_json({'frame': frame})
        player.send_frame = no_wait
        for _ in range(3):
            pcm = (np.sin(np.arange(24000 * 4) * .12) * 12000).astype('<i2').tobytes()
            expected_player = AirtelPlayback(Socket(), 'test')
            expected = expected_player.encode(pcm) + expected_player.encode(bytes(124))
            start = len(socket.messages)
            for offset in range(0, len(pcm), 514):
                await player.play(pcm[offset:offset+514], 0)
            await player.finish_turn(0)
            actual = b''.join(m['frame'] for m in socket.messages[start:])
            self.assertEqual(actual[:len(expected)], expected)
            self.assertLess(len(actual) - len(expected), 160)
            self.assertTrue(all(len(m['frame']) == 160 for m in socket.messages[start:]))

    async def test_background_energy_cannot_cancel_or_extend_closing_forever(self):
        socket = Socket()
        player = AirtelPlayback(socket, 'test')
        goodbye = Goodbye(player, asyncio.Queue(), socket, asyncio.Event(), grace=0)
        goodbye.request()
        goodbye.acoustic_activity()
        deadline = goodbye.acoustic_hold_until
        for _ in range(100):
            goodbye.acoustic_activity()
        self.assertEqual(goodbye.acoustic_hold_until, deadline)
        self.assertTrue(goodbye.requested)
        goodbye.acoustic_hold_until = 0
        goodbye.turn_complete()
        await asyncio.sleep(0)
        player.acknowledge(socket.messages[-1])
        for _ in range(10):
            await asyncio.sleep(0)
        self.assertTrue(goodbye.sent)
        goodbye.stopped()
        await goodbye.task


if __name__ == '__main__':
    unittest.main()
