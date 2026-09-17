"""BOT4U phone worker: Piopiy rooms <-> Gemini native audio.

Run with .local/piopiy-venv/Scripts/python.exe piopiy-worker.py.
Secrets stay in .env; account routing and scripts stay in .local.
"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent
for line in (ROOT / '.env').read_text(encoding='utf-8-sig').splitlines():
    if '=' in line and not line.lstrip().startswith('#'):
        key, value = line.split('=', 1)
        os.environ.setdefault(key.strip(), value.strip().strip('\"\''))

from google import genai
from google.genai import types
from livekit import rtc
from piopiy.agent import Agent, URL_CTX, TOKEN_CTX
from phone_control import finish_phone_call
from phone_history import save_session
from phone_recording import CallRecording
from datetime import datetime, timezone

OWNER = 'huzaifa'
MAPPING = json.loads((ROOT / '.local/telephony.json').read_text())[OWNER]
MODEL = os.environ.get('GEMINI_LIVE_MODEL', 'gemini-3.1-flash-live-preview')

def prompt(mode='inbound', name=''):
    owner_id = hashlib.sha256(OWNER.encode()).hexdigest()
    text = (ROOT / '.local/scripts' / owner_id / (mode + '.txt')).read_text(encoding='utf-8')
    if not text.strip():
        raise ValueError('Save an inbound script for Huzaifa before starting phone calls.')
    text = text.replace('{{customer_name}}', name).replace('{{customer_email}}', 'not provided')
    return text + '\n' + (ROOT / 'voice-defaults.txt').read_text(encoding='utf-8') + (
        '\nThis is a telephone call. There is no on-screen booking button. '
        'Do not refer to a screen or button. Customer name is unknown; do not invent it. '
        'Email delivery is unavailable on this phone connection; never claim an email was sent. '
        'If asked, provide the booking URL verbally. After your final spoken goodbye, '
        'call end_conversation. Never end while waiting for a customer response.'
    )

def config(mode='inbound', name=''):
    return types.LiveConnectConfig(
        response_modalities=['AUDIO'], system_instruction=prompt(mode, name),
        speech_config={'voice_config': {'prebuilt_voice_config': {'voice_name': 'Sulafat'}}},
        realtime_input_config={'automatic_activity_detection': {
            'start_of_speech_sensitivity': 'START_SENSITIVITY_HIGH',
            'end_of_speech_sensitivity': 'END_SENSITIVITY_HIGH',
            'prefix_padding_ms': 20, 'silence_duration_ms': 400},
            'activity_handling': 'START_OF_ACTIVITY_INTERRUPTS'},
        tools=[{'function_declarations': [{'name': 'end_conversation',
                'description': 'Disconnect after delivering your final spoken goodbye.'}]}],
    )

async def create_session(agent_id, call_id, from_number, to_number, metadata=None):
    if agent_id != MAPPING['agent_id']:
        return
    room = rtc.Room()
    source = rtc.AudioSource(24000, 1, queue_size_ms=120)
    ended = asyncio.Event()
    tasks = set()
    tracks = set()
    audio = asyncio.Queue(maxsize=1000)
    generation = 0
    recording = CallRecording(ROOT, OWNER)
    client = genai.Client(api_key=os.environ['GEMINI_API_KEY'])
    digits = lambda value: ''.join(c for c in str(value or '') if c.isdigit())
    mode = 'outbound' if digits(from_number) == MAPPING['caller_id'] else 'inbound'
    details = metadata if isinstance(metadata, dict) else {}
    name = str(details.get('customer_name', ''))[:80]
    started_at = datetime.now(timezone.utc).isoformat()
    connected_at = None
    remarks = 'Phone session ended.'
    try:
        async with client.aio.live.connect(model=MODEL, config=config(mode, name)) as session:
            async def forward(track):
                stream = rtc.AudioStream(track, sample_rate=16000, num_channels=1)
                try:
                    async for event in stream:
                        recording.add(bytes(event.frame.data), 16000, 0)
                        await session.send_realtime_input(audio=types.Blob(
                            data=bytes(event.frame.data), mime_type='audio/pcm;rate=16000'))
                finally:
                    await stream.aclose()

            def subscribe(track, publication, participant):
                if track.kind == rtc.TrackKind.KIND_AUDIO and track.sid not in tracks:
                    tracks.add(track.sid)
                    task = asyncio.create_task(forward(track))
                    tasks.add(task)

            room.on('track_subscribed', subscribe)
            room.on('participant_disconnected', lambda participant: ended.set())
            room.on('disconnected', lambda *args: ended.set())
            await room.connect(URL_CTX.get(), TOKEN_CTX.get())
            track = rtc.LocalAudioTrack.create_audio_track('BOT4U', source)
            options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
            await room.local_participant.publish_track(track, options)
            for participant in room.remote_participants.values():
                for pub in participant.track_publications.values():
                    if pub.track:
                        subscribe(pub.track, pub, participant)
            print('Phone room connected; Gemini audio ready.', flush=True)
            connected_at = time.monotonic()

            async def play():
                nonlocal remarks
                while True:
                    data = await audio.get()
                    try:
                        if data is None:
                            await finish_phone_call(source, call_id, os.environ['PIOPIY_API_TOKEN'])
                            print('Closing audio played; Piopiy hangup request accepted.', flush=True)
                            remarks = 'Closing statement played; Piopiy accepted the hangup request.'
                            ended.set()
                            return
                        # 20 ms PCM frames keep interruptions responsive.
                        epoch, pcm = data
                        for offset in range(0, len(pcm), 960):
                            if epoch != generation:
                                break
                            chunk = pcm[offset:offset + 960]
                            await source.capture_frame(rtc.AudioFrame(chunk, 24000, 1, len(chunk)//2))
                            recording.add(chunk, 24000, 1)
                    finally:
                        audio.task_done()

            async def receive():
                nonlocal generation
                while not ended.is_set():
                    async for response in session.receive():
                        content = response.server_content
                        if content and content.interrupted:
                            generation += 1
                            source.clear_queue()
                            while not audio.empty():
                                audio.get_nowait()
                                audio.task_done()
                        if response.data:
                            await audio.put((generation, response.data))
                        if response.tool_call:
                            for call in response.tool_call.function_calls:
                                if call.name == 'end_conversation':
                                    await audio.put(None)

            tasks.update([asyncio.create_task(play()), asyncio.create_task(receive()),
                          asyncio.create_task(ended.wait())])
            await session.send_realtime_input(text='The caller is connected. Give your opening greeting now.')
            done, _ = await asyncio.wait(tasks, timeout=300, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
    except asyncio.CancelledError:
        remarks = 'Phone session was cancelled or disconnected.'
        raise
    except Exception as exc:
        remarks = 'Phone session error: ' + type(exc).__name__
        print('Phone session failed: ' + type(exc).__name__, flush=True)
    finally:
        recording_id = None
        try:
            recording_id = recording.save()
        except Exception as exc:
            print('Recording could not be saved: ' + type(exc).__name__, flush=True)
        save_session(ROOT, OWNER, requestId=details.get('history_id'), callId=call_id,
                     startedAt=started_at, type=mode, phone='+' + digits(to_number if mode=='outbound' else from_number),
                     name=name, duration=round(time.monotonic()-connected_at) if connected_at else None,
                     result='Answered' if connected_at else 'Unconfirmed', remarks=remarks,
                     recordingId=recording_id)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await room.disconnect()
        await source.aclose()
        await client.aio.aclose()
        print('Phone session ended.', flush=True)

async def smoke_test():
    client = genai.Client(api_key=os.environ['GEMINI_API_KEY'])
    try:
        async with client.aio.live.connect(model=MODEL, config=config()) as session:
            await session.send_realtime_input(text='Say: Hello, this is BOT4U. Then stop.')
            async with asyncio.timeout(30):
                async for response in session.receive():
                    if response.data:
                        print('Gemini native audio test passed: received PCM audio.', flush=True)
                        return
            raise RuntimeError('No audio returned')
    finally:
        await client.aio.aclose()

async def main():
    prompt()
    if '--test-audio' in sys.argv:
        await smoke_test()
        return
    agent = Agent(agent_id=MAPPING['agent_id'],
                  agent_token=os.environ['PIOPIY_API_TOKEN'], create_session=create_session, debug=False)
    # SDK connect() installs Unix signal handlers, unsupported on Windows.
    async def heartbeat():
        while True:
            status = {'owner': OWNER, 'agent_id': MAPPING['agent_id'],
                      'connected': agent.sio.connected, 'updated_at': int(time.time()*1000)}
            target = ROOT / '.local/phone-worker.json'
            temporary = target.with_suffix('.tmp')
            temporary.write_text(json.dumps(status))
            temporary.replace(target)
            await asyncio.sleep(5)
    pulse = None
    try:
        await agent.sio.connect(agent.signaling_url, auth={
            'agent_id': agent.agent_id, 'token': agent.agent_token}, transports=['websocket'])
        print('Piopiy authenticated. BOT4U is listening for inbound calls on +91 79434 44692.', flush=True)
        pulse = asyncio.create_task(heartbeat())
        await agent.sio.wait()
    finally:
        if pulse:
            pulse.cancel()
            await asyncio.gather(pulse, return_exceptions=True)
        await agent.shutdown()
        (ROOT / '.local/phone-worker.json').unlink(missing_ok=True)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
