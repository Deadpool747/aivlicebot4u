import hashlib
import tempfile
import unittest
from pathlib import Path
from airtel_bot4u import matches, session_config, OWNER

class AirtelRoutingTest(unittest.TestCase):
    def test_only_assigned_number_routes_to_huzaifa(self):
        for number in ['+91 8045911978', '8045911978', '918045911978']:
            self.assertTrue(matches({'airtel_iq_called_number': number}))
        for number in ['918045911979', '', None, '917943444692']:
            self.assertFalse(matches({'airtel_iq_called_number': number}))
        self.assertFalse(matches({'airtel_iq_caller_number': '918045911978'}))

    def test_reads_latest_account_script_and_rejects_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / '.local/scripts' / hashlib.sha256(OWNER.encode()).hexdigest()
            folder.mkdir(parents=True)
            (root / '.env').write_text('GEMINI_API_KEY=test-only')
            (root / 'voice-defaults.txt').write_text('Speak naturally.')
            script = folder / 'inbound.txt'
            script.write_text('First script')
            self.assertIn('First script', session_config(root, 'inbound')[1].system_instruction)
            script.write_text('Changed script')
            self.assertIn('Changed script', session_config(root, 'inbound')[1].system_instruction)
            script.write_text(' ')
            with self.assertRaises(ValueError):
                session_config(root, 'inbound')

if __name__ == '__main__':
    unittest.main()
