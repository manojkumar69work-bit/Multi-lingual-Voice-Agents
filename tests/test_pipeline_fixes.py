"""Two pipeline faults found by a headless Tamil test call."""
import ast


def _load(names):
    ns = {"LEAD_EXTRACT_MODEL": "openai/gpt-oss-120b"}
    for n in ast.parse(open("agent.py").read()).body:
        if isinstance(n, ast.FunctionDef) and n.name in names:
            exec(compile(ast.Module([n], []), "agent.py", "exec"), ns)
    return ns


def test_sarvam_stream_asks_for_the_end_of_reply_event():
    # Without it Sarvam never says a reply is finished, and each reply waited
    # out the 20 s frame timeout before the next sentence could play.
    import tts_server
    assert "send_completion_event=true" in tts_server._sarvam_ws_url("bulbul:v3")


def test_lead_extraction_leaves_room_for_the_json():
    # 300 tokens with default reasoning truncated the JSON mid-object.
    body = _load({"_lead_extract_body"})["_lead_extract_body"]("PROMPT", "openai/gpt-oss-120b")
    assert body["max_tokens"] >= 800
    assert body["response_format"] == {"type": "json_object"}
    assert body["reasoning_effort"] == "low"


def test_lead_extraction_body_for_non_reasoning_models():
    body = _load({"_lead_extract_body"})["_lead_extract_body"]("PROMPT", "llama-3.3-70b-versatile")
    assert "reasoning_effort" not in body and body["messages"][0]["content"] == "PROMPT"
