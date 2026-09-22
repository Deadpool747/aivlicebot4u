"""Continuous telephone audio, playback acknowledgement and cancellable goodbye."""
import asyncio
import audioop
import base64
import logging
import uuid
import numpy as np


class AirtelPlayback:
    def __init__(self, websocket, stream_sid):
        self.websocket, self.stream_sid = websocket, stream_sid
        self.version = 0
        self.pending = bytearray()
        # Stateful anti-alias filter before 24 kHz -> 8 kHz decimation.
        x = np.arange(63) - 31
        self.kernel = np.sinc(2 * 3400 / 24000 * x) * np.hamming(63)
        self.kernel /= self.kernel.sum()
        self.tail = np.zeros(62)
        self.phase = 0
        self.next_frame = 0.0
        self.marks = {}
        self.frames_sent = 0
        self.underruns = 0

    def encode(self, pcm):
        samples = np.frombuffer(pcm, dtype='<i2').astype(float)
        if not len(samples):
            return b''
        filtered = np.convolve(np.concatenate((self.tail, samples)), self.kernel, mode='valid')
        self.tail = np.concatenate((self.tail, samples))[-62:]
        output = filtered[(-self.phase) % 3::3]
        self.phase = (self.phase + len(samples)) % 3
        return audioop.lin2ulaw(np.clip(output, -32768, 32767).astype('<i2').tobytes(), 2)

    async def play(self, pcm, version):
        if version != self.version:
            return
        self.pending.extend(self.encode(pcm))
        while len(self.pending) >= 160 and version == self.version:
            frame = bytes(self.pending[:160])
            del self.pending[:160]
            await self.send_frame(frame, version)

    async def send_frame(self, frame, version):
        loop = asyncio.get_running_loop()
        now = loop.time()
        if not self.next_frame:
            self.next_frame = now
        elif now - self.next_frame > 0.08:
            self.underruns += 1
            self.next_frame = now
        await asyncio.sleep(max(0, self.next_frame - now))
        if version != self.version:
            return
        await self.websocket.send_json({'event': 'media', 'streamSid': self.stream_sid,
            'media': {'payload': base64.b64encode(frame).decode('ascii')}})
        # Absolute media clock: scheduler/send overhead must not accumulate per packet.
        self.next_frame += 0.02
        self.frames_sent += 1

    async def finish_turn(self, version):
        if version != self.version:
            return
        # Drain filter tail, then pad only the final packet of a turn.
        self.pending.extend(self.encode(bytes(124)))
        while self.pending and version == self.version:
            frame = bytes(self.pending[:160])
            del self.pending[:160]
            await self.send_frame(frame.ljust(160, b'\xff'), version)
        self.tail[:] = 0
        self.phase = 0
        self.next_frame = 0

    async def clear(self):
        self.version += 1
        self.pending.clear()
        self.tail[:] = 0
        self.phase = 0
        self.next_frame = 0
        for event in self.marks.values():
            event.set()
        self.marks.clear()
        await self.websocket.send_json({'event': 'clear', 'streamSid': self.stream_sid})

    def acknowledge(self, message):
        name = (message.get('mark') or {}).get('name')
        if str(message.get('event', '')).lower() == 'mark' and name in self.marks:
            self.marks[name].set()

    async def wait_played(self, version):
        if version != self.version:
            return False
        name = 'bot4u-goodbye-' + uuid.uuid4().hex
        event = self.marks[name] = asyncio.Event()
        try:
            await self.websocket.send_json({'event': 'mark', 'streamSid': self.stream_sid,
                                           'mark': {'name': name}})
            await asyncio.wait_for(event.wait(), 15)
            return version == self.version
        finally:
            self.marks.pop(name, None)


class Goodbye:
    def __init__(self, playback, queue, websocket, ended, grace=1.2):
        self.playback, self.queue, self.websocket, self.ended = playback, queue, websocket, ended
        self.grace = grace
        self.requested = False
        self.task = None
        self.sent = False
        self.confirmed = False
        self.state = 'NORMAL_CONVERSATION'
        self.speaking = False
        self.allow_close = True
        self.cancelled_tasks = set()
        self.acoustic_hold_until = 0
        self.acoustic_checked = False

    def acoustic_activity(self):
        # Energy is not speech: permit recognition to arrive, but do not let
        # background noise permanently cancel a genuine completed goodbye.
        if self.requested and not self.acoustic_checked and not self.sent:
            self.acoustic_checked = True
            self.acoustic_hold_until = asyncio.get_running_loop().time() + 0.8

    def transition(self, state):
        if self.state != state:
            logging.getLogger('bot4u.airtel').info('Call state %s -> %s', self.state, state)
            self.state = state

    def request(self):
        if self.sent or self.speaking or not self.allow_close:
            return False
        if not self.requested:
            self.acoustic_checked = False
            self.acoustic_hold_until = 0
        self.requested = True
        self.transition('BOT_CLOSING')
        return True

    def stopped(self):
        self.confirmed = self.sent
        self.transition('CALL_TERMINATED')
        self.ended.set()


    def interrupt(self):
        # A terminate frame is a committed action; do not cancel its acknowledgement wait.
        if self.sent or self.state == 'CALL_TERMINATING':
            return
        closing = self.requested or self.task is not None
        self.requested = False
        if self.task:
            task = self.task
            task.cancel()
            self.cancelled_tasks.add(task)
            task.add_done_callback(self.cancelled_tasks.discard)
            self.task = None
        if closing:
            self.transition('CLOSING_CANCELLED')
        self.transition('NORMAL_CONVERSATION')

    def turn_complete(self):
        if self.requested and self.task is None and not self.sent:
            self.task = asyncio.create_task(self.finish(self.playback.version))

    async def finish(self, version):
        try:
            await self.queue.join()
            if not await self.playback.wait_played(version):
                return
            self.transition('WAITING_FOR_FINAL_CUSTOMER_RESPONSE')
            await asyncio.sleep(self.grace)
            await asyncio.sleep(max(0, self.acoustic_hold_until - asyncio.get_running_loop().time()))
            if not self.requested or self.speaking or version != self.playback.version:
                return
            self.transition('CALL_TERMINATING')
            await self.websocket.send_json({'event': 'terminate', 'streamSid': self.playback.stream_sid,
                                           'reason': {'code': 1, 'text': 'Conversation complete'}})
            self.sent = True
            logging.getLogger('bot4u.airtel').info('Closing playback acknowledged; Airtel terminate sent')
            # Give Airtel time to terminate the phone leg and send its stop event.
            try:
                await asyncio.wait_for(self.ended.wait(), 5)
            except asyncio.TimeoutError:
                logging.getLogger('bot4u.airtel').error('Airtel terminate unconfirmed: no stop event')
                self.ended.set()
        except asyncio.TimeoutError:
            # No playback acknowledgement: do not cut off unconfirmed audio.
            logging.getLogger('bot4u.airtel').warning('Airtel goodbye playback acknowledgement timed out')
            self.requested = False
            self.transition('NORMAL_CONVERSATION')
        except Exception:
            logging.getLogger('bot4u.airtel').exception('Airtel goodbye failed')
            self.requested = False


class SpeechActivity:
    """Cancel closing on sustained incoming speech before delayed transcription arrives."""
    def __init__(self, rate=16000):
        self.rate = rate
        self.voiced = 0
        self.quiet = 0
        self.active = False
        self.candidate = False

    def process(self, pcm):
        samples = np.frombuffer(pcm, dtype='<i2').astype(float)
        self.candidate = bool(len(samples) and np.sqrt(np.mean(samples * samples)) >= 240)
        onset = False
        if self.candidate:
            self.voiced += len(samples)
            self.quiet = 0
            if not self.active and self.voiced >= self.rate * 0.06:
                self.active = True
                onset = True
        else:
            self.quiet += len(samples)
            self.voiced = 0
            if self.quiet >= self.rate * 0.3:
                self.active = False
        return onset
