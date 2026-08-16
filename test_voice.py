"""Voice-path self-check. Run: python3 test_voice.py

Covers the text that reaches the voice model and the text that reaches the
transcript — the two places the naturalness work changed behaviour and where a
regression would be silent (a mispronounced call still "works").

agent.py's pure functions are loaded out of its AST rather than imported, the
same way test_telugu.py does it: importing agent.py pulls in livekit and httpx,
and these functions have no need of either.
"""
import ast
import re

# ── Load the pure helpers out of agent.py ────────────────────────────────────
_src = open("agent.py").read()
_ns = {"re": re}
_tree = ast.parse(_src)

# Module-level constants the functions below close over (prompt fragments and
# the Indic-run pattern). All are plain literals / re.compile calls.
_CONSTS = {
    "_INDIC_RUN", "_LEAD_FIELDS_HINT_TEMPLATE", "_CLOSING_HINT_TEMPLATE",
    "_STYLE_HINT", "_SCOPE_GUARD",
}
for _n in _tree.body:
    if isinstance(_n, ast.Assign) and any(
        getattr(t, "id", "") in _CONSTS for t in _n.targets
    ):
        exec(compile(ast.Module([_n], []), "agent.py", "exec"), _ns)

for _n in _tree.body:
    if isinstance(_n, ast.FunctionDef) and _n.name in (
        "_tidy_itrans", "romanize", "build_greeting", "build_system_prompt",
    ):
        exec(compile(ast.Module([_n], []), "agent.py", "exec"), _ns)

# romanize() needs the transliteration machinery agent.py imports at module level.
try:
    from indic_transliteration.sanscript import transliterate, DEVANAGARI, TELUGU, ITRANS
    _ns.update(
        transliterate=transliterate, DEVANAGARI=DEVANAGARI, TELUGU=TELUGU,
        ITRANS=ITRANS, _HAS_TRANSLITERATE=True,
    )
except ImportError:  # pragma: no cover
    raise SystemExit("skip: indic-transliteration not installed")

romanize = _ns["romanize"]
build_greeting = _ns["build_greeting"]
build_system_prompt = _ns["build_system_prompt"]

is_dev = lambda s: any("ऀ" <= c <= "ॿ" for c in s)
is_te = lambda s: any("ఀ" <= c <= "౿" for c in s)


class Tenant:
    """Minimal stand-in for tenants.TenantConfig."""
    def __init__(self, language="hi"):
        self.language = language
        self.agent_name = "Riya"
        self.name = "Acme Realty"
        self.role_description = "a real estate consultant"
        self.business_type = "real estate agency"
        self.lead_fields = "budget, location"
        self.closing_instructions = ""
        self.system_prompt = ""
        self.greeting = ""
        self.business_info = ""


# ── Hindi is Roman Hinglish, not pure Hindi ──────────────────────────────────
# The product is meant to sound like an urban Indian caller on the phone, which
# means Hindi in Roman letters mixed with English — not Devanagari, which pulls
# the LLM toward formal newsreader Hindi. The only Devanagari allowed in the
# prompt is the counter-example showing what NOT to write.
hi_prompt = build_system_prompt(Tenant("hi"))
assert "NEVER use Devanagari" in hi_prompt
assert "Main aapki madad karungi" in hi_prompt, "prompt must model Roman Hinglish"
# English loanwords are preferred outright, not merely tolerated.
assert "budget" in hi_prompt and "location" in hi_prompt
# Few-shot exchange: register drifts formal over a long call unless it's anchored
# by examples rather than rules alone.
assert "Ji bilkul! 3 BHK ke liye aapka budget kitna hai?" in hi_prompt

hi_greeting = build_greeting(Tenant("hi"))
assert not is_dev(hi_greeting), f"Hindi greeting must be Roman: {hi_greeting}"
assert "Namaste" in hi_greeting and "Main" in hi_greeting, hi_greeting

# ── Telugu is spoken Vaaduka, with English loanwords in Latin ────────────────
te_prompt = build_system_prompt(Tenant("te"))
assert is_te(te_prompt)
# Loanwords in English letters, matching the Hindi convention. The Telugu-script
# transliterations are what this replaced, so their presence is the regression.
assert "మీ budget ఎంత అండి?" in te_prompt
assert "appointment, time, free" in te_prompt
# The old rule glossed every loanword into Telugu script — "appointment
# (అపాయింట్‌మెంట్)", "budget (బడ్జెట్)". Those glosses are what got removed; the one
# surviving Telugu-script "బడ్జెట్" is the counter-example showing what NOT to write.
assert "(అపాయింట్‌మెంట్)" not in te_prompt and "(బడ్జెట్)" not in te_prompt, \
    "Telugu loanwords must be in Latin, not transliterated into Telugu script"
assert te_prompt.count("బడ్జెట్") == 1, "only the counter-example may use Telugu-script budget"
assert "Grandhikam" in te_prompt, "the anti-bookish rule must survive"
assert "3 BHK కి మీ budget ఎంత అనుకుంటున్నారు?" in te_prompt

assert is_te(build_greeting(Tenant("te")))
assert build_greeting(Tenant("en")).startswith("Hi!")


# ── The transcript gets readable Latin back ──────────────────────────────────
# romanize() keeps the client portal readable for Telugu calls, and stays as the
# safety net on Hindi ones — Whisper can still return Devanagari for the caller's
# side even though the agent now writes Roman. It must transliterate ONLY the
# Indic runs, and must be a no-op on text that is already Latin.
out = romanize("नमस्ते! मैं Riya बोल रही हूँ, आपकी real estate assistant.")
assert not is_dev(out), out
# English words survive byte for byte — transliterating the whole string used to
# lowercase and shred them ("BHK" → "bhk", "Riya" → "riy").
assert "Riya" in out and "real estate assistant" in out, out
# Long vowels survive: lowercasing before expanding ITRANS capitals turned
# हूँ into "hun" and आपका into "apaka".
assert "hoon" in out, out

out2 = romanize("मुझे 2 BHK के बारे में बताइए।")
assert "BHK" in out2 and "2" in out2, out2
assert "mein" in out2, out2

# Hindi drops the inherent final 'a' that ITRANS spells out.
out3 = romanize("आपका शुभ नाम क्या है?")
assert "shubh" in out3 and "naam" in out3, out3
assert "shubha" not in out3 and "nama " not in out3, out3

# Telugu KEEPS its final vowels — the same rule applied there would clip the end
# off most words (చెప్పండి is "cheppandi", never "cheppand").
te_out = romanize("నమస్కారం! చెప్పండి, మీ బడ్జెట్ ఎంత అండి?")
assert not is_te(te_out), te_out
assert "cheppandi" in te_out, te_out
assert "andi" in te_out, te_out

# Pure Latin passes through completely untouched, case included.
latin = "Hi! I'm Riya. What's your BHK budget?"
assert romanize(latin) == latin


# ── Numbers reaching the voice ───────────────────────────────────────────────
from tts_engine import normalize_text  # noqa: E402  (no livekit dependency)

# A 10-digit mobile is ten digits, not one number in the billions. num2words on
# the whole run produced something both wrong and impossible to write down.
phone = normalize_text("मेरा नंबर 9876543210 है", "hi")
assert "9876543210" not in phone, phone
assert len(phone.split()) > 10, f"phone should be spoken digit by digit: {phone}"

# Ordinary numbers, times and units are left as digits: Bulbul v3 always runs
# its own preprocessing and reads them correctly, while the old pass turned
# "10:30" into "दस:तीस" and "2 BHK" into "दो BHK".
assert "10:30" in normalize_text("मीटिंग 10:30 पर है", "hi")
assert "2 BHK" in normalize_text("2 BHK चाहिए", "hi")
assert "50" in normalize_text("50 लाख का budget", "hi")

# Currency and percent symbols are still expanded — the digits stay, only the
# symbol becomes a word. The magnitude word has to stay attached to the amount:
# every property price on these calls is quoted as "50 lakh rupees", and putting
# the currency first says "50 rupees lakh".
#
# The Hindi labels are Roman ("rupaye", "percent"), because the agent replies in
# Roman Hinglish — a Devanagari रुपये would be the one native-script word in an
# otherwise Latin sentence. Telugu replies are in Telugu script, so it keeps its.
rupees = normalize_text("keemat ₹50 lakh hai", "hi")
assert "50 lakh rupaye" in rupees, rupees
assert "₹" not in rupees, rupees
assert "2 crore rupaye" in normalize_text("budget ₹2 crore tak", "hi")
assert "50 లక్ష రూపాయలు" in normalize_text("ధర ₹50 లక్ష అండి", "te")
# No magnitude word: the label goes straight after the amount.
assert "75000 rupaye" in normalize_text("₹75000 per month", "hi")
pct = normalize_text("20% chhoot", "hi")
assert "percent" in pct and "20" in pct, pct
# Devanagari input still normalizes correctly — Whisper can return it for the
# caller's side, and /synthesize takes arbitrary text.
assert "50 लाख rupaye" in normalize_text("कीमत ₹50 लाख है", "hi")

# English is left alone entirely.
assert normalize_text("call me on 9876543210", "en") == "call me on 9876543210"

print("ok")
