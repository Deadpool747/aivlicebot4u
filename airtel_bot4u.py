"""Account-scoped Gemini session on the existing Airtel IQ media bridge."""
import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from google import genai
from google.genai import types
from phone_history import save_session
from phone_recording import CallRecording
from phone_audio import InputNoiseGate

ROOT = Path('/opt/bot4u')
OWNER = 'huzaifa'
NUMBER = '918045911978'
log = logging.getLogger('bot4u.airtel')

def digits(value):
    return ''.join(c for c in str(value or '') if c.isdigit())

def matches(context):
    return any(digits(context.get(k)) in (NUMBER, NUMBER[2:]) for k in
               ('airtel_iq_called_number', 'called_via_number'))

def session_config(root, mode, name=""):
    env = {}
    for line in (root / '.env').read_text(encoding='utf-8-sig').splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip().strip(chr(34) + chr(39))
    owner = hashlib.sha256(OWNER.encode()).hexdigest()
    script = (root / '.local/scripts' / owner / (mode + '.txt')).read_text(encoding='utf-8')
    if not script.strip():
        raise ValueError('Save your BOT4U script before receiving calls.')
    script = script.replace('{{customer_name}}', name).replace('{{customer_email}}', 'not provided')
    script += '\n' + (root / 'voice-defaults.txt').read_text(encoding='utf-8')
    script += ('\nThis is a telephone call. No screen or booking button is visible. '
               'Do not claim an email or booking was completed. After your final spoken goodbye, '
               'call end_conversation. If interrupted, respond instead of ending. '
               'Never end while waiting for the customer to answer a question.')
    return env, types.LiveConnectConfig(response_modalities=['AUDIO'], system_instruction=script,
        input_audio_transcription={}, output_audio_transcription={},
        speech_config={'voice_config': {'prebuilt_voice_config': {'voice_name': 'Sulafat'}}},
        realtime_input_config={'automatic_activity_detection': {
            'start_of_speech_sensitivity': 'START_SENSITIVITY_LOW',
            'end_of_speech_sensitivity': 'END_SENSITIVITY_LOW',
            'prefix_padding_ms': 120, 'silence_duration_ms': 650}},
        tools=[{'function_declarations': [{'name': 'end_conversation',
            'description': 'End the telephone conversation after the final spoken goodbye.'}]}])

async def run(websocket, bridge, context, call_id, root=ROOT):
    from phone_provider import selected_provider
    if selected_provider(root, OWNER) != 'airtel_iq':
        await websocket.send_json({'event': 'terminate', 'streamSid': bridge.stream_sid,
                                   'reason': {'code': 1, 'text': 'Carrier not selected'}})
        await websocket.close()
        return
    mode = 'outbound' if context.get('direction') == 'outbound' else 'inbound'
    started = datetime.now(timezone.utc).isoformat()
    clock = time.monotonic()
    recording = CallRecording(root, OWNER)
    ready, ended = asyncio.Event(), asyncio.Event()
    if bridge.stream_sid:
        ready.set()
    queue = asyncio.Queue(maxsize=1000)
    tasks = []
    closer = None
    player = None
    client = None
    epoch = 0
    connected = False
    remarks = 'Airtel call ended.'

    async def media():
        while True:
            frame = await websocket.receive()
            if frame.get('type') == 'websocket.disconnect':
                ended.set()
                return
            payload = frame.get('text')
            if not payload:
                continue
            try:
                message = json.loads(payload)
            except (ValueError, TypeError):
                continue
            if not isinstance(message, dict):
                continue
            await bridge.handle_ws_message(message)
            if bridge.stream_sid:
                ready.set()
            event = str(message.get('event') or message.get('eventType') or message.get('status') or '').lower()
            if event in {'stop', 'terminate', 'stream_terminate', 'streamstop', 'end', 'error'}:
                ended.set()
                return

    async def play():
        while True:
            version, pcm = await queue.get()
            try:
                # One small frame at a time permits genuine caller interruption.
                for offset in range(0, len(pcm), 960):
                    if version != epoch:
                        break
                    chunk = pcm[offset:offset+960]
                    await bridge.play(chunk)
                    recording.add(chunk, 24000, 1)
            finally:
                queue.task_done()

    try:
        env, config = session_config(root, mode, context.get('name', ''))
        client = genai.Client(api_key=env['GEMINI_API_KEY'])
        tasks.append(asyncio.create_task(media()))
        await asyncio.wait_for(ready.wait(), 15)
        async with client.aio.live.connect(model=env.get('GEMINI_LIVE_MODEL', 'gemini-3.1-flash-live-preview'), config=config) as session:
            connected = True
            log.info('Airtel call connected to BOT4U account huzaifa')
            async def forward():
                gate = InputNoiseGate()
                async for pcm in bridge.mic_chunks():
                    recording.add(pcm, 16000, 0)
                    await session.send_realtime_input(audio=types.Blob(data=gate.process(pcm), mime_type='audio/pcm;rate=16000'))
                ended.set()

            async def close_after_audio():
                nonlocal remarks
                await queue.join()
                await bridge.wait_for_playback_idle()
                await asyncio.sleep(0.8)
                await websocket.send_json({'event': 'terminate', 'streamSid': bridge.stream_sid, 'reason': {'code': 1, 'text': 'Conversation complete'}})
                remarks = 'BOT4U closing audio finished; Airtel hangup requested.'
                ended.set()

            async def receive():
                nonlocal epoch, closer
                while not ended.is_set():
                    async for response in session.receive():
                        content = response.server_content
                        if content and (content.interrupted or (content.input_transcription and content.input_transcription.text)):
                            if closer:
                                closer.cancel()
                                closer = None
                        if content and content.interrupted:
                            epoch += 1
                            await bridge.flush_playback()
                        if response.data:
                            await queue.put((epoch, response.data))
                        if response.tool_call:
                            for call in response.tool_call.function_calls:
                                if call.name == 'end_conversation' and closer is None:
                                    closer = asyncio.create_task(close_after_audio())
                                await session.send_tool_response(function_responses=[types.FunctionResponse(
                                    id=call.id, name=call.name, response={'status': 'closing_after_audio'})])

            player = asyncio.create_task(play())
            tasks += [player, asyncio.create_task(forward()), asyncio.create_task(receive()), asyncio.create_task(ended.wait())]
            await session.send_realtime_input(text='The caller is connected. Give your opening greeting now.')
            done, _ = await asyncio.wait(tasks, timeout=300, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
    except Exception as exc:
        remarks = 'Airtel BOT4U error: ' + type(exc).__name__
        log.error(remarks)
    finally:
        if closer:
            closer.cancel()
            tasks.append(closer)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            recording_id = recording.save()
            save_session(root, OWNER, requestId=context.get('historyId'), callId=call_id, startedAt=started, type=mode,
                phone=context.get('number') or ('+' + digits(context.get('airtel_iq_caller_number')) if digits(context.get('airtel_iq_caller_number')) else ''), name=context.get('name', ''),
                duration=round(time.monotonic()-clock), result='Answered' if connected else 'Unconfirmed',
                remarks=remarks, recordingId=recording_id, provider='airtel_iq')
        except Exception:
            log.exception('Could not save BOT4U Airtel history')
        if client:
            await client.aio.aclose()
        try:
            await websocket.close()
        except RuntimeError:
            pass
