"""Account-scoped Gemini session on the existing Airtel IQ media bridge."""
import asyncio
import hashlib
import json
import logging
import time
import re
from datetime import datetime, timezone
from pathlib import Path
from google import genai
from google.genai import types
from phone_history import save_session
from phone_recording import CallRecording
from phone_audio import InputNoiseGate
from airtel_playback import AirtelPlayback, Goodbye, SpeechActivity

ROOT = Path('/opt/bot4u')
OWNER = 'huzaifa'
NUMBER = '918045911978'
log = logging.getLogger('bot4u.airtel')

def digits(value):
    return ''.join(c for c in str(value or '') if c.isdigit())

def matches(context):
    return any(digits(context.get(k)) in (NUMBER, NUMBER[2:]) for k in
               ('airtel_iq_called_number', 'called_via_number'))

def session_config(root, mode, name="", follow_up=None):
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
               'Never end while waiting for the customer to answer a question. '
               'Closing is reversible. If the customer speaks after a goodbye, listen to their latest '
               'request and answer it normally. Wait, actually, another question, explain, order, '
               'documents, or requests for details are continuation, NOT goodbye confirmation. '
               'Do not repeat thanks or goodbye in response to a new question. Only close again '
               'when the customer genuinely ends the conversation. A closing tool result only '
               'means a tentative request, never that the customer has finished. '
               'After calling end_conversation do not generate another spoken farewell.')
    if follow_up:
        script += ('\nThis call is a scheduled follow-up. Address this purpose naturally without reading metadata aloud: '
                   + json.dumps({'reason': follow_up.get('reason', ''), 'notes': follow_up.get('notes', ''),
                                 'previous_summary': follow_up.get('previousSummary', '')}, ensure_ascii=False))
    script += ('\nIf the customer asks for a callback or says to call later, do not ask for a date, time, timezone, name, or phone number. '
               'Simply acknowledge with a brief phrase such as "Okay, we will follow up," then call mark_follow_up_requested. '
               'Do not claim that a specific callback has been scheduled.')
    return env, types.LiveConnectConfig(response_modalities=['AUDIO'], system_instruction=script,
        input_audio_transcription={}, output_audio_transcription={},
        speech_config={'voice_config': {'prebuilt_voice_config': {'voice_name': 'Sulafat'}}},
        realtime_input_config={'automatic_activity_detection': {
            'start_of_speech_sensitivity': 'START_SENSITIVITY_LOW',
            'end_of_speech_sensitivity': 'END_SENSITIVITY_LOW',
            'prefix_padding_ms': 120, 'silence_duration_ms': 650}},
        tools=[{'function_declarations': [{'name': 'mark_follow_up_requested',
            'description': 'Record the call remark as Follow up when the customer asks to be called back. Do not collect scheduling details.'},
            {'name': 'end_conversation',
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
    playback = AirtelPlayback(websocket, bridge.stream_sid)
    goodbye = Goodbye(playback, queue, websocket, ended)
    player = None
    client = None
    connected = False
    remarks = 'Airtel call ended.'
    follow_up_requested = False

    async def media():
        while True:
            frame = await websocket.receive()
            if frame.get('type') == 'websocket.disconnect':
                goodbye.stopped()
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
            playback.acknowledge(message)
            await bridge.handle_ws_message(message)
            if bridge.stream_sid:
                ready.set()
            event = str(message.get('event') or message.get('eventType') or message.get('status') or '').lower()
            if event in {'stop', 'terminate', 'stream_terminate', 'streamstop', 'end', 'error'}:
                if event == 'stop':
                    goodbye.stopped()
                else:
                    ended.set()
                return

    async def play():
        buffered_version = None
        while True:
            version, pcm = await queue.get()
            try:
                if pcm is None:
                    await playback.finish_turn(version)
                    buffered_version = None
                elif version == playback.version:
                    if buffered_version != version:
                        # A small initial cushion absorbs Gemini/network chunk jitter.
                        await asyncio.sleep(0.12)
                        buffered_version = version
                    await playback.play(pcm, version)
                    recording.add(pcm, 24000, 1)
            finally:
                queue.task_done()

    try:
        env, config = session_config(root, mode, context.get('name', ''), context.get('followUp'))
        client = genai.Client(api_key=env['GEMINI_API_KEY'])
        tasks.append(asyncio.create_task(media()))
        await asyncio.wait_for(ready.wait(), 15)
        playback.stream_sid = bridge.stream_sid
        async with client.aio.live.connect(model=env.get('GEMINI_LIVE_MODEL', 'gemini-3.1-flash-live-preview'), config=config) as session:
            connected = True
            log.info('Airtel call connected to BOT4U account huzaifa')
            activity = SpeechActivity()
            last_speech = 0.0
            async def forward():
                nonlocal last_speech
                gate = InputNoiseGate()
                async for pcm in bridge.mic_chunks():
                    recording.add(pcm, 16000, 0)
                    filtered = gate.process(pcm)
                    onset = activity.process(filtered)
                    if activity.active:
                        last_speech = time.monotonic()
                    if onset:
                        goodbye.acoustic_activity()
                    await session.send_realtime_input(audio=types.Blob(data=filtered, mime_type='audio/pcm;rate=16000'))
                ended.set()

            async def receive():
                nonlocal remarks, follow_up_requested
                output_text = ''
                input_text = ''
                turn_audio = False
                while not ended.is_set():
                    async for response in session.receive():
                        content = response.server_content
                        if goodbye.sent:
                            continue
                        voice_activity = getattr(response, 'voice_activity', None)
                        activity_type = str(getattr(voice_activity, 'voice_activity_type', '') or '')
                        if activity_type.endswith('ACTIVITY_START'):
                            goodbye.speaking = True
                            goodbye.interrupt()
                            log.info('Gemini customer speech start')
                        elif activity_type.endswith('ACTIVITY_END'):
                            goodbye.speaking = False
                        if content and content.interrupted:
                            goodbye.interrupt()
                            log.info('Gemini interruption; recent local speech=%s', time.monotonic() - last_speech < 1)
                            await playback.clear()
                            output_text = ''
                            turn_audio = False
                        elif content and content.input_transcription and content.input_transcription.text:
                            # Cancel a pending goodbye for actual caller speech.
                            goodbye.interrupt()
                            input_text += content.input_transcription.text
                            # Questions/continuations override farewell words within the same utterance.
                            continuation = bool(re.search(r'\?|\b(wait|actually|question|what|when|why|how|explain|more|order|documents)\b', input_text, re.I))
                            goodbye.allow_close = not continuation
                        if content and content.output_transcription and content.output_transcription.text:
                            output_text += content.output_transcription.text
                        if response.data:
                            if not turn_audio:
                                log.info('Gemini audio turn started')
                            turn_audio = True
                            await queue.put((playback.version, response.data))
                        if response.tool_call:
                            for call in response.tool_call.function_calls:
                                tool_result = None
                                if call.name == 'end_conversation':
                                    goodbye.request()
                                    tool_result = {'status': 'pending_silence' if goodbye.requested else 'cancelled_customer_continuing',
                                        'instruction': 'Listen and answer any new customer question normally; do not repeat a goodbye.'}
                                elif call.name == 'mark_follow_up_requested':
                                    follow_up_requested = True
                                    remarks = 'Follow up'
                                    tool_result = {'status': 'recorded', 'remark': 'Follow up'}
                                await session.send_tool_response(function_responses=[types.FunctionResponse(
                                    id=call.id, name=call.name, response=tool_result or {'status': 'unsupported'})])
                        if content and content.turn_complete:
                            goodbye.speaking = False
                            # Some model turns speak a farewell without invoking the tool.
                            from phone_signals import closing
                            if turn_audio and closing(output_text) and goodbye.allow_close:
                                goodbye.request()
                            await queue.put((playback.version, None))
                            goodbye.turn_complete()
                            log.info('Gemini turn complete; audio=%s closing=%s queue=%s frames=%s underruns=%s',
                                     turn_audio, goodbye.requested, queue.qsize(), playback.frames_sent, playback.underruns)
                            # A normal answer has now been generated. A later genuine goodbye is allowed.
                            if turn_audio and not closing(output_text):
                                goodbye.allow_close = True
                            output_text, input_text, turn_audio = '', '', False

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
        if follow_up_requested:
            remarks = 'Follow up'
        elif goodbye.sent:
            remarks = ('Airtel termination confirmed by stream stop.' if goodbye.confirmed else
                       'Airtel hangup requested; provider termination not confirmed.')
        if goodbye.task:
            goodbye.task.cancel()
            tasks.append(goodbye.task)
        tasks.extend(goodbye.cancelled_tasks)
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
