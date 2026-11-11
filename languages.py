"""Language registry: the single list of languages the agent can speak.

Every language here is one Sarvam covers end to end — Saaras v3 for streaming
code-mixed STT, Bulbul v3 for TTS — so adding a row is enough to make a
language selectable in the admin portal, the test caller and the TTS service.

Two-letter codes are the tenant-facing key (`tenant.language`); Sarvam wants
them as `<code>-IN`. Odia is "od" on both sides, which is Sarvam's spelling,
not ISO's "or".

Speakers are Sarvam's own per-language recommendations for bulbul:v3 (ranked on
character error rate in that language). Override one with TTS_VOICE_<CODE>.
Edge voices are the last-resort fallback; Odia and Punjabi have no Indian Edge
voice, so they fall back to the Hindi one — a change of language, but not dead
air.

Greetings end by asking the caller's name, the same shape as the hi/te/en
built-ins in agent.py, with the loanword "assistant" left in Latin so the
first line matches the register the system prompt asks for.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Language:
    code: str            # tenant-facing key, e.g. "ta"
    name: str            # English label
    native: str          # label in its own script
    sarvam_code: str     # Sarvam language code, e.g. "ta-IN"
    speaker_female: str  # bulbul:v3 speaker
    speaker_male: str    # bulbul:v3 speaker
    edge_voice: str      # Edge TTS fallback voice
    script: tuple[str, str] | None  # Unicode block (first, last) of the native script; None for Latin
    greeting: str        # first line; {agent_name} and {biz_type} are filled in
    unavailable: str     # spoken when a client is over quota

    @property
    def speaker(self) -> str:
        return os.environ.get(f"TTS_VOICE_{self.code.upper()}", self.speaker_female)


_ROWS = [
    Language("hi", "Hinglish", "Hindi · Roman", "hi-IN", "priya", "shubh", "hi-IN-SwaraNeural",
             ("ऀ", "ॿ"),
             "Namaste! Main {agent_name} bol rahi hoon, aapki {biz_type} assistant. Aapka shubh naam kya hai?",
             "Maaf kijiye, abhi service uplabdh nahi hai. Kripya baad mein call karein."),
    Language("te", "Telugu", "తెలుగు", "te-IN", "neha", "shubh", "te-IN-ShrutiNeural",
             ("ఀ", "౿"),
             "నమస్కారం! నేను {agent_name}ని, మీ {biz_type} assistantని. చెప్పండి, మీ పేరు ఏంటి?",
             "క్షమించండి, ప్రస్తుతం service అందుబాటులో లేదు. దయచేసి తర్వాత call చేయండి."),
    Language("en", "English", "English", "en-IN", "ishita", "ratan", "en-IN-NeerjaNeural",
             None,
             "Hi! I'm {agent_name}, your {biz_type} assistant. What's your name?",
             "Sorry, the service is temporarily unavailable. Please call back later."),
    Language("ta", "Tamil", "தமிழ்", "ta-IN", "ishita", "ratan", "ta-IN-PallaviNeural",
             ("஀", "௿"),
             "வணக்கம்! நான் {agent_name}, உங்கள் {biz_type} assistant. சொல்லுங்க, உங்க பெயர் என்ன?",
             "மன்னிக்கவும், இப்போது service கிடைக்கவில்லை. தயவுசெய்து பிறகு call செய்யுங்கள்."),
    Language("kn", "Kannada", "ಕನ್ನಡ", "kn-IN", "neha", "shubh", "kn-IN-SapnaNeural",
             ("ಀ", "೿"),
             "ನಮಸ್ಕಾರ! ನಾನು {agent_name}, ನಿಮ್ಮ {biz_type} assistant. ಹೇಳಿ, ನಿಮ್ಮ ಹೆಸರು ಏನು?",
             "ಕ್ಷಮಿಸಿ, ಈಗ service ಲಭ್ಯವಿಲ್ಲ. ದಯವಿಟ್ಟು ನಂತರ call ಮಾಡಿ."),
    Language("ml", "Malayalam", "മലയാളം", "ml-IN", "pooja", "shubh", "ml-IN-SobhanaNeural",
             ("ഀ", "ൿ"),
             "നമസ്കാരം! ഞാൻ {agent_name}, നിങ്ങളുടെ {biz_type} assistant. പറയൂ, നിങ്ങളുടെ പേര് എന്താണ്?",
             "ക്ഷമിക്കണം, ഇപ്പോൾ service ലഭ്യമല്ല. ദയവായി പിന്നീട് call ചെയ്യൂ."),
    Language("mr", "Marathi", "मराठी", "mr-IN", "priya", "ratan", "mr-IN-AarohiNeural",
             ("ऀ", "ॿ"),
             "नमस्कार! मी {agent_name}, तुमची {biz_type} assistant. सांगा, तुमचं नाव काय?",
             "माफ करा, सध्या service उपलब्ध नाही. कृपया नंतर call करा."),
    Language("bn", "Bengali", "বাংলা", "bn-IN", "roopa", "rehan", "bn-IN-TanishaaNeural",
             ("ঀ", "৿"),
             "নমস্কার! আমি {agent_name}, আপনার {biz_type} assistant. বলুন, আপনার নাম কী?",
             "দুঃখিত, এখন service পাওয়া যাচ্ছে না। অনুগ্রহ করে পরে call করুন।"),
    Language("gu", "Gujarati", "ગુજરાતી", "gu-IN", "priya", "ratan", "gu-IN-DhwaniNeural",
             ("઀", "૿"),
             "નમસ્તે! હું {agent_name}, તમારી {biz_type} assistant. કહો, તમારું નામ શું છે?",
             "માફ કરશો, હાલ service ઉપલબ્ધ નથી. કૃપા કરીને પછી call કરો."),
    Language("pa", "Punjabi", "ਪੰਜਾਬੀ", "pa-IN", "roopa", "mani", "hi-IN-SwaraNeural",
             ("਀", "੿"),
             "ਸਤ ਸ੍ਰੀ ਅਕਾਲ! ਮੈਂ {agent_name}, ਤੁਹਾਡੀ {biz_type} assistant. ਦੱਸੋ, ਤੁਹਾਡਾ ਨਾਮ ਕੀ ਹੈ?",
             "ਮਾਫ਼ ਕਰਨਾ, ਹੁਣ service ਉਪਲਬਧ ਨਹੀਂ ਹੈ। ਕਿਰਪਾ ਕਰਕੇ ਬਾਅਦ ਵਿੱਚ call ਕਰੋ।"),
    Language("od", "Odia", "ଓଡ଼ିଆ", "od-IN", "ritu", "shubh", "hi-IN-SwaraNeural",
             ("଀", "୿"),
             "ନମସ୍କାର! ମୁଁ {agent_name}, ଆପଣଙ୍କ {biz_type} assistant. କୁହନ୍ତୁ, ଆପଣଙ୍କ ନାମ କ'ଣ?",
             "କ୍ଷମା କରନ୍ତୁ, ବର୍ତ୍ତମାନ service ଉପଲବ୍ଧ ନାହିଁ। ଦୟାକରି ପରେ call କରନ୍ତୁ।"),
]

LANGUAGES: dict[str, Language] = {row.code: row for row in _ROWS}
DEFAULT = "hi"


def codes() -> list[str]:
    """Language codes in display order."""
    return [row.code for row in _ROWS]


def get(code: str | None) -> Language:
    """The registry row for `code`, or the default language when unknown."""
    return LANGUAGES.get((code or "").strip().lower(), LANGUAGES[DEFAULT])


def normalize(code: str | None) -> str:
    """A known language code, falling back to the default."""
    return get(code).code


def labels() -> dict[str, str]:
    """code → English label, for UI pickers."""
    return {row.code: row.name for row in _ROWS}


def public_list() -> list[dict]:
    """What /api/languages returns: enough for a picker, nothing vendor-specific."""
    return [{"code": row.code, "name": row.name, "native": row.native} for row in _ROWS]


def script_of(ch: str) -> str | None:
    """Language code whose native script contains `ch`, if any.

    Devanagari is shared by Hindi and Marathi; it reports as "hi"."""
    for row in _ROWS:
        if row.script and row.script[0] <= ch <= row.script[1]:
            return row.code
    return None
