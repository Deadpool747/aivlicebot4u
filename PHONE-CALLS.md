# BOT4U phone worker

The standalone worker connects Huzaifa's assigned Piopiy agent to Gemini native audio. It reads the account mapping from `.local/telephony.json` and reloads Huzaifa's saved inbound script for every incoming call. Secrets are read from `.env` and are not committed.

Required environment values: `PIOPIY_API_TOKEN`, `GEMINI_API_KEY`; optional `GEMINI_LIVE_MODEL`.

New calls are recorded locally as stereo WAV audio (caller and bot on separate channels). After the call ends, use Play recording in the call dashboard to open the player. Files are saved under the owner's `.local/call-history` directory and served only through the authenticated recording endpoint. Previous calls cannot be reconstructed. Recordings remain locally until removed by the operator.

Windows commands from this folder:

```powershell
.local/piopiy-venv/Scripts/python.exe piopiy-worker.py --test-audio
.local/piopiy-venv/Scripts/python.exe piopiy-worker.py
```

The worker must remain running and the computer must remain awake. It does not start automatically after a reboot. A successful signaling connection verifies authentication, not telephone audio. Make an inbound call to verify two-way audio and interruptions. Do not run competing workers for the same agent during testing.

Select Outbound in the browser, save the outbound script, enter a number with + and country code, and click Call. The authenticated server submits the call using the account's assigned caller ID and agent. The worker chooses the outbound script when the originating number is the assigned Piopiy number. A fresh worker heartbeat is required, and repeat requests to the same destination are blocked for one minute; a different destination can be called once the previous request finishes submitting. A submitted request does not confirm that the recipient answered.

Phone email delivery is unavailable, and its prompt states this explicitly. When Gemini calls end_conversation after its goodbye, the worker drains speech playback, requests hangup of the current call through Piopiy's call API, then leaves the audio room. An accepted hangup request is logged; carrier disconnection must be confirmed with a real call.
