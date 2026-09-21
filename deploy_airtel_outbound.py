from pathlib import Path
import shutil

target = Path('/opt/new_voice_agent/voice_sales_agent/web_app.py')
source = target.read_text()
old = "                    if not context['airtel_iq_called_number'] or bot4u_matches(context):"
new = "                    from airtel_context import resolve\n                    context = resolve(context, message)\n                    if context.get('direction') == 'outbound' or not context['airtel_iq_called_number'] or bot4u_matches(context):"
if new not in source:
    assert source.count(old) == 1, 'Unexpected media handler'
    updated = source.replace(old, new)
    compile(updated, str(target), 'exec')
    shutil.copy2(target, target.with_suffix('.py.before-bot4u-outbound'))
    target.write_text(updated)
print('Outbound context dispatch installed')
