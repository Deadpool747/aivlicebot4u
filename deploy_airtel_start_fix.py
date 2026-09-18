"""Route Airtel starts before legacy pending-call lookup can discard them."""
from pathlib import Path
import shutil

MARKER = '# The shared Airtel endpoint is assigned to Huzaifa'


def patched_source(source):
    start = source.index('    async def _run_airtel_iq_media_session(')
    end = source.index('    @app.websocket', start)
    section = source[start:end]
    if MARKER in section:
        return source
    anchor = '                await bridge.handle_ws_message(message)'
    assert section.count(anchor) == 1, 'Unexpected Airtel adapter; no changes made'
    hook = '''
                # The shared Airtel endpoint is assigned to Huzaifa, including
                # inbound start events that have no legacy pending-call record.
                if not pending_id and event in {'streaminfo', 'streamstart', 'start'} and bridge.stream_sid:
                    import sys
                    if '/opt/bot4u' not in sys.path:
                        sys.path.append('/opt/bot4u')
                    from airtel_bot4u import matches as bot4u_matches, run as bot4u_run
                    details = _extract_airtel_iq_start_details(message)
                    context = {'direction': 'inbound',
                        'airtel_iq_called_number': details.get('called_number', ''),
                        'airtel_iq_caller_number': details.get('caller_number', '')}
                    if not context['airtel_iq_called_number'] or bot4u_matches(context):
                        logger.info('Dispatching Airtel start directly to BOT4U huzaifa')
                        await bot4u_run(websocket, bridge, context, bridge.call_sid or bridge.stream_sid)
                        return
'''
    updated = source[:start] + section.replace(anchor, anchor + hook) + source[end:]
    compile(updated, '<airtel-adapter>', 'exec')
    return updated


if __name__ == '__main__':
    target = Path('/opt/new_voice_agent/voice_sales_agent/web_app.py')
    source = target.read_text()
    updated = patched_source(source)
    if updated == source:
        print('Direct Airtel start dispatch already installed')
    else:
        backup = target.with_suffix('.py.before-airtel-start-fix')
        assert not backup.exists(), 'Existing backup must be reviewed first'
        shutil.copy2(target, backup)
        target.write_text(updated)
        print('Direct Airtel start dispatch installed')
