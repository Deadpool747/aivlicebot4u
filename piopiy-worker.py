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
from phone_audio import InputNoiseGate
from phone_signals import voicemail, human_greeting, closing
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
        'Do not refer to a screen or button. '
        + ('Use the provided customer name naturally. ' if name else 'Customer name is unknown; do not invent it. ')
        +
        'Email delivery is unavailable on this phone connection; never claim an email was sent. '
        'If asked, provide the booking URL verbally. After your final spoken goodbye, '
        'call end_conversation. End your final closing with Goodbye (or the equivalent in the customer language). Never end while waiting for a customer response. '
        'If interrupted during goodbye, answer the new question instead of ending. '
        'For outbound calls: briefly listen first. When instructed by the application, greet the customer even if they have not spoken yet. Never hang up solely because no greeting was detected. '
        'If you hear voicemail, an answering machine, a beep, or an automated call-screening request, call voicemail_detected silently. Never leave a message or introduce yourself to a screening system.'
    )

def config(mode='inbound', name=''):
    return types.LiveConnectConfig(
        response_modalities=['AUDIO'], system_instruction=prompt(mode, name),
        input_audio_transcription={}, output_audio_transcription={},
        speech_config={'voice_config': {'prebuilt_voice_config': {'voice_name': 'Sulafat'}}},
        realtime_input_config={'automatic_activity_detection': {
            'start_of_speech_sensitivity': 'START_SENSITIVITY_LOW',
            'end_of_speech_sensitivity': 'END_SENSITIVITY_LOW',
            'prefix_padding_ms': 120, 'silence_duration_ms': 650},
            'activity_handling': 'START_OF_ACTIVITY_INTERRUPTS'},
        tools=[{'function_declarations': [{'name': 'end_conversation',
                'description': 'Disconnect after delivering your final spoken goodbye.'},
                {'name': 'voicemail_detected', 'description': 'Silently disconnect a voicemail, answering machine or automated call-screening system. Do not speak.'}]}],
    )

async def create_session(agent_id, call_id, from_number, to_number, metadata=None):
    from phone_provider import selected_provider
    if selected_provider(ROOT, OWNER) != 'piopiy':
        print('Piopiy session ignored: another carrier is selected.', flush=True)
        return
    if agent_id != MAPPING['agent_id']:
        print('Phone invite rejected: agent ID does not match configured mapping.', flush=True)
        return
    print('Phone session callback received for configured agent.', flush=True)
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
    human = mode != 'outbound'
    machine = False
    human_candidate = None
    no_human = False
    closing_task = None
    close_version = 0
    output_text = ''
    input_text = ''
    try:
        async with client.aio.live.connect(model=MODEL, config=config(mode, name)) as session:
            async def forward(track):
                stream = rtc.AudioStream(track, sample_rate=16000, num_channels=1)
                noise_gate = InputNoiseGate()
                try:
                    async for event in stream:
                        recording.add(bytes(event.frame.data), 16000, 0)
                        await session.send_realtime_input(audio=types.Blob(
                            data=noise_gate.process(bytes(event.frame.data)), mime_type='audio/pcm;rate=16000'))
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

            def cancel_closing():
                nonlocal close_version, closing_task
                close_version += 1
                if closing_task and not closing_task.done():
                    closing_task.cancel()
                closing_task = None

            def request_close(silent=False):
                nonlocal closing_task, machine, remarks, generation
                if closing_task and not closing_task.done():
                    return
                version = close_version
                if silent:
                    machine = True
                    generation += 1
                    source.clear_queue()
                    while not audio.empty():
                        audio.get_nowait()
                        audio.task_done()

                async def close_after_audio():
                    nonlocal remarks
                    await audio.join()
                    for attempt in range(3):
                        try:
                            accepted = await finish_phone_call(
                                source, call_id, os.environ['PIOPIY_API_TOKEN'],
                                still_valid=lambda: silent or close_version == version,
                                grace=0 if silent else 0.8)
                            if accepted:
                                remarks = (('No live greeting detected within 15 seconds; disconnected silently.' if no_human else 'Voicemail / automated screening detected; disconnected without a message.')
                                           if machine else 'Closing audio finished; carrier hangup accepted.')
                                print(remarks, flush=True)
                                ended.set()
                            return
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            print('Hangup attempt failed: ' + type(exc).__name__, flush=True)
                            if attempt == 2:
                                remarks = 'Carrier hangup failed after three attempts.'
                                ended.set()
                                return
                            await asyncio.sleep(1)
                closing_task = asyncio.create_task(close_after_audio())

            async def answer_timeout():
                nonlocal human
                await asyncio.sleep(5)
                if not human and not machine and not ended.is_set():
                    human = True
                    await session.send_realtime_input(text='No automated greeting was detected. Say a brief Hello, can you hear me? now, then wait for the customer. Do not end the call merely because the customer was initially silent.')
                await ended.wait()

            async def receive():
                nonlocal generation, human, input_text, output_text, human_candidate
                while not ended.is_set():
                    async for response in session.receive():
                        content = response.server_content
                        if content and content.input_transcription:
                            spoken = content.input_transcription.text or ""
                            input_text = (input_text + spoken)[-1000:]
                            if mode == "outbound" and voicemail(input_text):
                                request_close(silent=True)
                            elif not human and spoken.strip():
                                if human_candidate:
                                    human_candidate.cancel()
                                async def confirm_greeting():
                                    nonlocal human
                                    await asyncio.sleep(1.2)
                                    if not machine and input_text.strip() and not voicemail(input_text):
                                        human = True
                                        await session.send_realtime_input(text='A live human greeting was detected. Give your short opening now.')
                                human_candidate = asyncio.create_task(confirm_greeting())
                            if spoken.strip() and not machine:
                                cancel_closing()
                        if content and content.output_transcription:
                            output_text += content.output_transcription.text or ""
                        if content and content.interrupted:
                            if not machine:
                                cancel_closing()
                            output_text = ""
                            generation += 1
                            source.clear_queue()
                            while not audio.empty():
                                audio.get_nowait()
                                audio.task_done()
                        if response.data and human and not machine:
                            await audio.put((generation, response.data))
                        if response.tool_call:
                            for call in response.tool_call.function_calls:
                                if call.name == 'end_conversation':
                                    request_close()
                                elif call.name == 'voicemail_detected':
                                    request_close(silent=True)
                                await session.send_tool_response(function_responses=[types.FunctionResponse(
                                    id=call.id, name=call.name, response={'status': 'scheduled'})])
                        if content and content.turn_complete:
                            if human and closing(output_text):
                                request_close()
                            output_text = ''
                            if human:
                                input_text = ''

            tasks.update([asyncio.create_task(play()), asyncio.create_task(receive()),
                          asyncio.create_task(ended.wait())])
            if mode == 'outbound':
                tasks.add(asyncio.create_task(answer_timeout()))
            await session.send_realtime_input(text=(
                'Outbound call connected. Briefly listen for a greeting. The application will prompt you to speak if the customer is initially quiet. Hang up silently only on clearly detected voicemail or automated screening.'
                if mode == 'outbound' else 'The caller is connected. Give your opening greeting now.'))
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
                     result='Not answered' if machine else ('Answered' if connected_at else 'Unconfirmed'), remarks=remarks,
                     recordingId=recording_id)
        if human_candidate:
            human_candidate.cancel()
            await asyncio.gather(human_candidate, return_exceptions=True)
        if closing_task:
            closing_task.cancel()
            await asyncio.gather(closing_task, return_exceptions=True)
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
    # Trace delivery without logging room tokens, caller details or invite payloads.
    join_handler = agent.sio.handlers['/']['join_room']
    async def traced_join(*args):
        print('Piopiy join_room event received.', flush=True)
        try:
            return await join_handler(*args)
        except Exception as exc:
            print('Piopiy join_room dispatch failed: ' + type(exc).__name__, flush=True)
            raise
    agent.sio.on('join_room', handler=traced_join)
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
