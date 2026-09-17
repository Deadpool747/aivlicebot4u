import unittest
from phone_control import finish_phone_call


class HangupTests(unittest.IsolatedAsyncioTestCase):
    async def test_playback_finishes_before_hangup(self):
        events = []

        class Source:
            async def wait_for_playout(self):
                events.append('played')

        class Response:
            def raise_for_status(self):
                pass
            def json(self):
                return {'code': 200}

        class Client:
            def __init__(self, **kwargs):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def post(self, url, **kwargs):
                self_test.assertEqual(events, ['played'])
                self_test.assertEqual(kwargs['json'], {'call_id': 'current-call', 'cause': 'NORMAL_CLEARING'})
                self_test.assertTrue(url.endswith('/voice/call/hangup'))
                events.append('hangup')
                return Response()

        self_test = self
        self.assertTrue(await finish_phone_call(Source(), 'current-call', 'test-token', Client))
        self.assertEqual(events, ['played', 'hangup'])

    async def test_missing_call_id_does_not_send_request(self):
        class Source:
            async def wait_for_playout(self):
                pass
        with self.assertRaises(RuntimeError):
            await finish_phone_call(Source(), None, 'test-token')


if __name__ == '__main__':
    unittest.main()
