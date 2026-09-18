import json

def selected_provider(root, owner):
    try:
        mapping = json.loads((root / '.local/telephony.json').read_text())[owner]
        return mapping.get('outbound_provider', mapping.get('provider', 'piopiy'))
    except (OSError, ValueError, KeyError):
        return None
