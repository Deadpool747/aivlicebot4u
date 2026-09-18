"""Install the scoped Airtel bridge hook on the existing Ubuntu deployment."""
from pathlib import Path
import shutil

target = Path('/opt/new_voice_agent/voice_sales_agent/web_app.py')
source = target.read_text()
start = source.index('    async def _run_airtel_iq_media_session(')
end = source.index('    @app.websocket', start)
section = source[start:end]
anchor = '            if not session_started and event in {"connected", "streaminfo", "streamstart", "start", "media"}:'
hook = '''            # BOT4U owns only the explicitly assigned Airtel number.
            import sys
            if '/opt/bot4u' not in sys.path:
                sys.path.append('/opt/bot4u')
            from airtel_bot4u import matches as bot4u_matches, run as bot4u_run
            if bot4u_matches(pending_call.metadata or {}):
                await bot4u_run(websocket, bridge, pending_call.metadata or {}, active_pending_id)
                return

'''
assert section.count(anchor) == 1, 'Unexpected deployed Airtel adapter; no changes made'
assert 'bot4u_run' not in section, 'Hook already installed'
updated = source[:start] + section.replace(anchor, hook + anchor) + source[end:]
compile(updated, str(target), 'exec')
backup = target.with_suffix('.py.before-bot4u-airtel')
assert not backup.exists(), 'Existing backup must be reviewed first'
shutil.copy2(target, backup)
target.write_text(updated)
print('Scoped Airtel hook installed; original source backed up.')
