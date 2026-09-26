import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from follow_up_client import schedule_follow_up


class Response:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps({'followUp': {'id': 'one', 'scheduledAt': '2030-01-01T04:30:00.000Z',
                                        'timezone': 'Asia/Kolkata'}}).encode()


class FollowUpClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmation_is_required(self):
        result = await schedule_follow_up(Path('.'), 'huzaifa', {'confirmed': False})
        self.assertEqual(result['status'], 'error')

    async def test_sends_local_authenticated_request(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.local').mkdir()
            (root / '.local/follow-up-internal-token').write_text('secret')
            with patch('follow_up_client.request.urlopen', return_value=Response()) as opened:
                result = await schedule_follow_up(root, 'huzaifa', {
                    'confirmed': True, 'date': '2030-01-01', 'time': '10:00',
                    'timezone': 'Asia/Kolkata', 'reason': 'Requested'},
                    {'customerName': 'Priya', 'phoneNumber': '+919876543210'})
            sent = json.loads(opened.call_args.args[0].data)
            self.assertEqual(sent['owner'], 'huzaifa')
            self.assertEqual(sent['phoneNumber'], '+919876543210')
            self.assertEqual(opened.call_args.args[0].headers['X-bot4u-internal-token'], 'secret')
            self.assertEqual(result['status'], 'scheduled')


if __name__ == '__main__':
    unittest.main()
