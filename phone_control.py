"""End the telephone leg only after the closing audio has played."""
import asyncio
import httpx


async def finish_phone_call(source, call_id, token, client_factory=httpx.AsyncClient):
    await source.wait_for_playout()
    if not call_id:
        raise RuntimeError('Missing Piopiy call ID; cannot hang up the telephone leg')
    # Allow the last audio frame to cross the phone transport.
    await asyncio.sleep(0.25)
    async with client_factory(timeout=10) as client:
        response = await client.post(
            'https://rest.piopiy.com/v3/voice/call/hangup',
            headers={'Authorization': 'Bearer ' + token},
            json={'call_id': call_id, 'cause': 'NORMAL_CLEARING'},
        )
        response.raise_for_status()
        body = response.json()
        if body.get('error') or body.get('success') is False:
            raise RuntimeError('Piopiy rejected hangup')
        if body.get('code') and body['code'] not in (200, '200', 'cmi-200'):
            raise RuntimeError('Piopiy rejected hangup')
    return True
