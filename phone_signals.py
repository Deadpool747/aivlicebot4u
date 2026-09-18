"""Conservative transcript signals; machine detection is best effort."""
import re


def voicemail(text):
    return bool(re.search(
        r'leave (?:a |your )?(?:voice ?)?message|after (?:the )?(?:beep|tone)|'
        r'voice ?mail|mailbox|not available.*(?:call|message)|'
        r'state your name|record your name|reason for (?:your )?call|'
        r'संदेश (?:छोड़|छोड|द्या)|बीप के बाद|बीप नंतर', text, re.I))


def human_greeting(text):
    return not voicemail(text) and bool(re.fullmatch(
        r'\s*(?:hello|hi|hey|yes|speaking|hello who is this|who is this|'
        r'हेलो|हॅलो|नमस्ते|नमस्कार|हाँ|हां|हो|बोलिए|बोला)[\s.!?।]*', text, re.I))


def closing(text):
    # Require an explicit farewell at the end, not "thank you" mid-conversation.
    return bool(re.search(
        r'(?:goodbye|bye(?: bye)?|have a (?:great|nice|good) day|take care|'
        r'अलविदा|आपका दिन शुभ हो|तुमचा दिवस शुभ जावो)[\s.!।]*$', text, re.I)) and '?' not in text
