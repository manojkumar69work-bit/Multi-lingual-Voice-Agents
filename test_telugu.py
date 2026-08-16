"""Telugu STT-path self-check. Run: python3 test_telugu.py

Loads the functions straight out of agent.py's AST instead of importing it —
agent.py pulls in livekit/httpx at module level and these three functions are
pure, so there's nothing to gain from standing the whole worker up.
"""
import ast
import re

_ns = {"re": re}
for _n in ast.parse(open("agent.py").read()).body:
    if isinstance(_n, ast.FunctionDef) and _n.name in (
        "_build_stt_prompt", "_significant_words", "_is_bias_echo"
    ):
        exec(compile(ast.Module([_n], []), "agent.py", "exec"), _ns)

build_prompt = _ns["_build_stt_prompt"]
words = _ns["_significant_words"]
is_echo = _ns["_is_bias_echo"]

is_te = lambda s: any("ఀ" <= c <= "౿" for c in s)

# Whisper copies the prompt's script into its output, so a Telugu call needs a
# Telugu-script bias prompt — but names stay Latin so it spells them back right,
# and so do the English loanwords, matching the register the agent replies in.
te = build_prompt("Riya", "", "Acme", "te")
assert is_te(te), te
assert not is_te(build_prompt("Riya", "", "Acme", "hi"))
assert "Riya" in te

# Tokenizer must keep Telugu words whole. Bare \w drops combining marks and
# shreds అపాయింట్‌మెంట్ into 1-letter fragments, which silently kills echo
# detection on every Telugu call.
assert words("అపాయింట్‌మెంట్ టైమ్ బడ్జెట్") == [
    "అపాయింట్‌మెంట్", "టైమ్", "బడ్జెట్",
]
assert words("hello world 42x") == ["hello", "world", "42x"]

# A regurgitated prompt is dropped; a real caller's words are not. The sample
# tracks the prompt's own loanword list, which is now Latin apart from ధర/రేటు.
assert is_echo("appointment, time, ధర, రేటు, budget, location", te)
assert not is_echo("నా పేరు రమేష్, నాకు ఇల్లు కావాలి", te)
assert not is_echo("okay", te)

print("ok")
