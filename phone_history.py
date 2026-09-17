import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4


def save_session(root, owner, **fields):
    folder = root / '.local/call-history' / hashlib.sha256(owner.lower().encode()).hexdigest()
    folder.mkdir(parents=True, exist_ok=True)
    event_id = str(uuid4())
    record = dict(id=event_id, kind='session', at=datetime.now(timezone.utc).isoformat(), **fields)
    temporary = folder / (event_id + '.tmp')
    temporary.write_text(json.dumps(record), encoding='utf-8')
    temporary.replace(folder / (event_id + '.json'))
