import unittest
from phone_signals import closing, voicemail, human_greeting


class SignalsTests(unittest.TestCase):
    def test_voicemail_and_screening(self):
        for text in ['Please leave a message after the tone', 'Please state your name and reason for your call', 'You reached voicemail']:
            self.assertTrue(voicemail(text))
            self.assertFalse(human_greeting(text))

    def test_live_greetings(self):
        for text in ['Hello!', 'नमस्कार', 'हॅलो', 'Yes']:
            self.assertTrue(human_greeting(text))

    def test_only_final_farewells(self):
        self.assertTrue(closing('Thank you for your time. Goodbye.'))
        self.assertFalse(closing('Thank you. How can I help?'))
        self.assertFalse(closing('Goodbye is what the customer said.'))


if __name__ == '__main__':
    unittest.main()
