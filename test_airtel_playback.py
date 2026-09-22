import asyncio
import base64
import unittest
import numpy as np
from airtel_playback import AirtelPlayback, Goodbye


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


if __name__ == '__main__':
    unittest.main()
