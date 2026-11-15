import wave

import numpy as np

import recorder


def _read(path):
    with wave.open(path) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def test_agent_speech_delivered_in_a_burst_is_laid_end_to_end(tmp_path, monkeypatch):
    """TTS hands over a reply faster than real time. Each chunk must follow the
    previous one, as it does on the caller's speaker, not land on top of it."""
    monkeypatch.setattr(recorder, "RECORDINGS_DIR", str(tmp_path))
    rec = recorder.CallRecorder("burst")
    sr = recorder.SAMPLE_RATE
    chunk = np.full(sr, 1000, dtype=np.int16)      # 1 s of speech per chunk
    for _ in range(3):                              # all three arrive at once
        rec.add_agent_pcm(chunk.tobytes(), sr)

    out = _read(rec.finalize())

    assert len(out) >= 3 * sr
    assert out.max() == 1000                        # no chunks summed together


def test_agent_speech_after_a_pause_starts_at_the_current_time(tmp_path, monkeypatch):
    monkeypatch.setattr(recorder, "RECORDINGS_DIR", str(tmp_path))
    clock = [100.0]
    monkeypatch.setattr(recorder.time, "monotonic", lambda: clock[0])
    rec = recorder.CallRecorder("pause")
    sr = recorder.SAMPLE_RATE
    rec.add_agent_pcm(np.full(sr, 1000, dtype=np.int16).tobytes(), sr)
    clock[0] += 5.0                                 # caller talks for 5 s
    rec.add_agent_pcm(np.full(sr, 1000, dtype=np.int16).tobytes(), sr)

    out = _read(rec.finalize())

    assert out[int(5.5 * sr)] == 1000               # second reply at 5 s, not 1 s
    assert out[int(2.5 * sr)] == 0
