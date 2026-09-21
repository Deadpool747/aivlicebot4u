"""Match a streamed outbound call to a server-created Airtel request."""
import json
import time
from pathlib import Path

def resolve(context, message, root=Path('/opt/bot4u')):
    def digits(value):
        return ''.join(c for c in str(value or '') if c.isdigit())[-10:]
    start = message.get('start') or {}
    custom = start.get('customParams', start.get('customParameters', {}))
    if isinstance(custom, str):
        try:
            custom = json.loads(custom)
        except ValueError:
            custom = {}
    request_id = custom.get('bot4u_request_id') if isinstance(custom, dict) else None
    # A call to our DID is inbound; do not steal it for an outstanding request.
    called = digits(context.get('airtel_iq_called_number'))
    if called == '8045911978' and not request_id:
        return context
    numbers = {digits(context.get('airtel_iq_called_number')), digits(context.get('airtel_iq_caller_number'))} - {''}
    candidates = []
    for file in (root / '.local/airtel-pending').glob('*.json'):
        try:
            record = json.loads(file.read_text())
            if record.get('owner') != 'huzaifa' or record.get('expiresAt', 0) < time.time()*1000:
                continue
            if (request_id and file.stem == request_id) or (not request_id and digits(record.get('number')) in numbers):
                candidates.append((file, record))
        except (ValueError, OSError):
            continue
    if len(candidates) != 1:
        return context
    file, record = candidates[0]
    claimed = file.with_suffix('.claimed')
    try:
        file.rename(claimed)
    except OSError:
        return context
    return {**context, **record, 'direction': 'outbound'}
