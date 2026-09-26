"""Local-only client used by phone workers to schedule account follow-ups."""
import asyncio
import json
import os
from urllib import request, error


async def schedule_follow_up(root, owner, arguments, defaults=None):
    data = dict(arguments or {})
    if data.pop('confirmed', False) is not True:
        return {'status': 'error', 'error': 'Confirm the exact callback details with the customer first.'}
    for key, value in (defaults or {}).items():
        if not data.get(key) and value:
            data[key] = value
    data['owner'] = owner
    token = (root / '.local/follow-up-internal-token').read_text(encoding='utf-8').strip()
    url = os.environ.get('BOT4U_INTERNAL_URL', 'http://127.0.0.1:4174/api/internal/follow-ups')

    def send():
        payload = json.dumps(data).encode()
        req = request.Request(url, data=payload, method='POST', headers={
            'Content-Type': 'application/json', 'X-BOT4U-Internal-Token': token})
        try:
            with request.urlopen(req, timeout=8) as response:
                return json.loads(response.read())
        except error.HTTPError as exc:
            try:
                detail = json.loads(exc.read()).get('error')
            except (ValueError, AttributeError):
                detail = None
            return {'status': 'error', 'error': detail or 'The follow-up could not be scheduled.'}
        except (OSError, ValueError):
            return {'status': 'error', 'error': 'The follow-up service is temporarily unavailable.'}

    result = await asyncio.to_thread(send)
    if result.get('followUp'):
        item = result['followUp']
        return {'status': 'scheduled', 'id': item.get('id'), 'scheduledAt': item.get('scheduledAt'),
                'timezone': item.get('timezone')}
    return result
