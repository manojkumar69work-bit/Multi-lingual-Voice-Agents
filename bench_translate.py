"""Does direct Indic→Indic translation actually beat pivoting through English?

The bet this project is making: Telugu and Tamil are both verb-final (SOV), so a
translator can commit to output *before* the source sentence ends. English is
SVO, so any route through English has to wait for the Telugu verb — which arrives
last — before it can emit the English subject-verb. Every shipping product pivots
through English and eats that wait.

If the bet is right, two things show up in the numbers:

  1. PREFIX STABILITY is high for te→ta and low for te→en. Feed the translator a
     growing prefix of the sentence; if what it already emitted survives unchanged
     as more words arrive, you can speak it immediately. If it keeps rewriting,
     you can't speak anything until the speaker stops.

  2. TIME TO FIRST AUDIO is lower for the direct incremental route, measured from
     when the speaker STARTS talking — not from when they stop, which is the
     measurement vendors quote to make consecutive translation look fast.

If stability for te→ta is not clearly above te→en, the thesis is wrong and the
cheap thing to do is find that out here rather than after building the pipeline.

Usage:  python bench_translate.py            # full run, ~2 min, a few cents
        python bench_translate.py --no-tts   # stability only, no Sarvam calls
"""
from __future__ import annotations

from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()

import argparse
import os
import statistics
import sys
import time

import httpx

GROQ_BASE = os.environ.get("GROQ_BASE", "https://api.groq.com/openai/v1")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
# NB: llama-3.3-70b-versatile — the default in agent.py:144 — has been
# decommissioned by Groq and now 404s. Benchmarked against a live model.
CHAT_MODEL = os.environ.get("BENCH_MODEL", "openai/gpt-oss-120b")

# Conversational speech runs ~2.5-3 words/sec. Telugu sits at the low end because
# the words are longer. This is what makes the streaming simulation honest: a
# 12-word sentence takes ~4.3s to say, and that is the budget a real simultaneous
# translator is spending against.
WORDS_PER_SEC = 2.8

# Groq's free tier rate-limits on burst. Spacing calls keeps the sample complete
# rather than truncated at whatever point the 429 lands.
PACE_SLEEP = float(os.environ.get("BENCH_SLEEP", "1.5"))

# Real business-call utterances, all verb-final, all the shape this product would
# actually see. Held in Telugu script the way Whisper returns it.
SENTENCES = [
    "మా దగ్గర Kondapur లో మూడు bedroom flat ఒకటి available గా ఉంది",
    "మీరు చెప్పిన budget లో ఆ property దొరకడం కొంచెం కష్టం అవుతుంది అండి",
    "సార్ మీ site visit ని రేపు ఉదయం పదకొండు గంటలకు schedule చేసుకుందామా",
    "ఈ project కి bank loan approval already వచ్చేసింది కాబట్టి మీకు ఇబ్బంది ఉండదు",
    "ఆ area లో ధరలు గత సంవత్సరం తో పోలిస్తే ఇరవై శాతం పెరిగాయి",
    "మీ పేరు మరియు phone number ఒకసారి చెబితే నేను details పంపిస్తాను",
    "ఆ flat కి registration charges వేరుగా చెల్లించాల్సి ఉంటుంది సార్",
    "మేము ఇచ్చే possession date కి ఎలాంటి delay ఉండదని hundred percent guarantee",
    "మీరు investment కోసం చూస్తున్నారా లేక సొంతంగా ఉండటానికి కొంటున్నారా",
    "ఆ builder గురించి market లో మంచి పేరు ఉంది కాబట్టి risk తక్కువ",
]


MAX_ATTEMPTS = 4

# gpt-oss-120b is a reasoning model: max_tokens caps reasoning + content
# together, and its trace for one short sentence runs ~700-850 tokens. At the 200
# this script originally sent, `finish_reason` came back "length" with content
# EMPTY — and that empty string was being counted as the model declining to
# commit. It was our own cap. Tamil script also costs more tokens per character
# than Latin, so the direct route hit the ceiling more often than the pivot,
# which is very likely the whole of the "direct fails twice as often" result the
# first run reported. Truncation is now measured separately and loudly (see
# Turn.truncated) because a truncated sample is not a finding about a route.
MAX_TOKENS = int(os.environ.get("BENCH_MAX_TOKENS", "1024"))


@dataclass(frozen=True)
class Turn:
    """One translation and how it ended."""
    text: str
    seconds: float
    truncated: bool = False   # finish_reason == "length": our budget, not their choice


class CallFailed(RuntimeError):
    """A translation call that did not survive its retries. Costs one sentence,
    not the whole run — a 20-minute benchmark that dies at sentence 24 and prints
    nothing is worse than one that reports 29 of 30."""


class CallFailed(RuntimeError):
    """A translation call that did not survive its retries. Costs one sentence,
    not the whole run — a 20-minute benchmark that dies at sentence 24 and prints
    nothing is worse than one that reports 29 of 30."""


def _post(url: str, headers: dict, payload: dict, *, timeout: float = 60.0) -> dict:
    """POST with bounded retries on 429/5xx. Bounded matters: the previous
    version recursed on 429 with no depth limit, so a sustained rate limit was
    an infinite loop rather than an error you could read."""
    last = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            r = httpx.post(url, headers=headers, json=payload, timeout=timeout)
        except httpx.HTTPError as e:
            last = f"{type(e).__name__}: {e}"
        else:
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code}: {r.text[:200]}"
            if r.status_code == 429:
                wait = float(r.headers.get("retry-after", 8) or 8)
                time.sleep(min(wait, 30))
                continue
            if r.status_code < 500:
                break  # 400s do not fix themselves — a bad model name, a dead key
        time.sleep(min(2 ** attempt, 20))
    raise CallFailed(last)


def _chat(prompt: str, text: str) -> Turn:
    """One completion.

    Elapsed is wall-clock request time only — the pacing sleep is taken after the
    clock stops, so the latency numbers are not inflated by our own rate limiting.
    """
    t0 = time.perf_counter()
    data = _post(
        f"{GROQ_BASE}/chat/completions",
        {"Authorization": f"Bearer {GROQ_API_KEY}"},
        {
            "model": CHAT_MODEL,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": text},
            ],
            # Deterministic: we are measuring the route, not sampling noise.
            "temperature": 0.0,
            "max_tokens": MAX_TOKENS,
        },
    )
    choice = data["choices"][0]
    out = (choice["message"]["content"] or "").strip()
    truncated = choice.get("finish_reason") == "length"
    dt = time.perf_counter() - t0
    time.sleep(PACE_SLEEP)
    return Turn(text=out, seconds=dt, truncated=truncated)

# The instruction that makes incremental translation possible at all: the model
# must be told it is seeing a fragment, and told never to revise what it already
# committed. Without the second half it happily rewrites the whole sentence every
# time a word arrives, which is exactly the flicker that forces you to wait.
_INCREMENTAL = (
    "You are a simultaneous interpreter translating {src} to {tgt}. "
    "You will receive a PARTIAL sentence that is still being spoken. "
    "Translate only what you can commit to with certainty. It is correct to "
    "output less than the input if the rest depends on words not yet spoken. "
    "Never revise or re-order what you have already translated. "
    "Output only the {tgt} translation, nothing else."
)

_FULL = (
    "Translate the following {src} text to {tgt}. "
    "Output only the translation, nothing else."
)

LANG = {"te": "Telugu", "ta": "Tamil", "en": "English"}


# Indic sentence terminators as well as ASCII: a Tamil or Telugu output ending
# in a danda would otherwise count as a different word from the same word
# without one, and score a spurious rewrite.
_PUNCT = ".,!?;:।॥\"'“”‘’()"


def prefix_overlap(prev: str, curr: str) -> float:
    """Fraction of the previous output that survives as a prefix of the new one.

    1.0 means everything already said is still valid — safe to have spoken it.
    0.0 means the translator threw away its previous output, so speaking early
    would have put a wrong sentence in the listener's ear.
    """
    if not prev:
        return 1.0
    p, c = prev.split(), curr.split()
    kept = 0
    for a, b in zip(p, c):
        if a.strip(_PUNCT).lower() != b.strip(_PUNCT).lower():
            break
        kept += 1
    return kept / len(p)


def _is_degenerate(out: str) -> bool:
    """Nothing came back, or the model emitted a dangling fragment ending
    mid-word or mid-clause. Neither is a translation you could speak."""
    return not out or out.rstrip().endswith(("-", "‑", ",", "،"))


def _cut_points(n_words: int, steps: int) -> list[int]:
    """Word counts at which to interrupt the speaker.

    Starts at 40% — below that there is nothing committable in any language pair
    and the number just measures the model refusing to guess. Deduplicated,
    because on a short sentence two fractions round to the same cut and feeding
    the identical fragment twice scores a free 1.0 that measures nothing but
    determinism.
    """
    if steps < 2:
        return [max(2, int(n_words * 0.4))]
    fracs = [0.4 + 0.6 * i / (steps - 1) for i in range(steps)]
    cuts = sorted({min(n_words, max(2, int(n_words * f))) for f in fracs})
    return cuts


@dataclass
class StabilityRun:
    target: str
    mean: float
    scores: list[float] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    degenerate: int = 0
    truncated: int = 0
    n_cuts: int = 0
    error: str | None = None


def stability_run(sentence: str, tgt: str, *, steps: int = 5) -> StabilityRun:
    """Feed growing prefixes; report mean prefix stability across steps."""
    words = sentence.split()
    cuts = _cut_points(len(words), steps)
    run = StabilityRun(target=tgt, mean=0.0, n_cuts=len(cuts))

    # The last output we could actually have spoken. A degenerate step does NOT
    # reset this: the question a listener cares about is whether the words
    # already in their ear survived, and they survive or not across the gap. The
    # previous version reset it to the empty output, which quietly dropped the
    # comparison and shrank the sample for whichever route failed more often —
    # rewarding unreliability.
    last_good = ""
    for n in cuts:
        frag = " ".join(words[:n])
        sysmsg = _INCREMENTAL.format(src=LANG["te"], tgt=LANG[tgt])
        try:
            turn = _chat(sysmsg, frag)
        except CallFailed as e:
            run.error = str(e)
            run.steps.append({"words": n, "of": len(words), "error": str(e)})
            continue

        out = turn.text
        rec: dict = {"words": n, "of": len(words), "out": out,
                     "latency_s": round(turn.seconds, 3)}
        # Truncation is an instrument fault, not a result. Counted apart from
        # degeneracy and excluded from scoring: charging a route for our own
        # token budget is how the first run concluded direct was less reliable.
        if turn.truncated:
            run.truncated += 1
            rec["truncated"] = True
            run.steps.append(rec)
            continue
        if _is_degenerate(out):
            run.degenerate += 1
            rec["degenerate"] = True
            run.steps.append(rec)
            continue
        if last_good:
            score = prefix_overlap(last_good, out)
            run.scores.append(score)
            rec["stability"] = round(score, 3)
        run.steps.append(rec)
        last_good = out

    run.mean = statistics.mean(run.scores) if run.scores else 0.0
    return run


_tts = None


def _tts_engine():
    """The module-level synthesizer, built once.

    tts_engine constructs its own singleton at import, so the previous version's
    per-call `TTSSynthesizer()` was building a third and fourth copy of every
    provider — including MMS, which loads a model — for each of 90 measurements.
    """
    global _tts
    if _tts is not None:
        return _tts
    import tts_engine

    # Sarvam Bulbul v3 covers Tamil, but this project only ever configured
    # hi/te/en, so ta falls through to the Hindi profile and would be spoken by a
    # Hindi voice. Injected here rather than committed to DEFAULT_VOICES because
    # which speaker sounds right in Tamil is a listening call, not a spec —
    # audition before shipping. DEFAULT_VOICES is read at synthesis time, so
    # patching the imported module is enough for its singleton to see this.
    if "ta" not in tts_engine.DEFAULT_VOICES:
        tts_engine.DEFAULT_VOICES["ta"] = tts_engine.VoiceProfile(
            language="ta",
            sarvam_speaker=os.environ.get("TTS_VOICE_TA", "priya"),
            sarvam_lang="ta-IN",
            edge_voice="ta-IN-PallaviNeural",
            quality_tier=99,  # unaudited
        )
    _tts = tts_engine.synthesizer
    return _tts


def timed_synth(text: str, lang: str) -> tuple[float, str, int]:
    """Time to get audio bytes back. Returns (seconds, provider, n_bytes).

    synthesize() returns (wav, provider) — a tuple, which is truthy even when
    the bytes are empty, so the old `if not audio` check could never fire and a
    silent result would have been recorded as a fast one. The provider name
    comes back with the timing because TTSSynthesizer falls through
    Sarvam → Edge → MMS on failure, and a row timed against a different
    provider than its neighbours is not comparable to them.
    """
    last = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        # Paced OUTSIDE the timed region. Part 2 fires three syntheses per
        # sentence back to back; unpaced, Sarvam starts refusing them and the
        # row is lost to our own burst rather than to anything about the route.
        time.sleep(PACE_SLEEP)
        t0 = time.perf_counter()
        try:
            wav, provider = _tts_engine().synthesize(text, lang, preferred="sarvam")
            dt = time.perf_counter() - t0
        except Exception as e:  # noqa: BLE001 — provider chain raises bare RuntimeError
            last = f"{type(e).__name__}: {e}"
        else:
            if wav:
                return dt, provider, len(wav)
            last = f"no audio returned for {lang}"
        # A retry gets a fresh clock. Folding the failed attempt's wall time into
        # the measurement would report our retry policy as the model's latency.
        time.sleep(min(2 ** attempt, 20))
    raise RuntimeError(last)


class Truncated(RuntimeError):
    """A translation cut off by our own token budget.

    Raised before the text can reach TTS. Sending an empty translation to the
    synthesizer made every provider fail and printed "All TTS providers failed
    for lang=ta", which sent the first investigation of this after a TTS bug
    that did not exist.
    """


def _guard(turn: Turn, leg: str) -> None:
    if turn.truncated:
        raise Truncated(f"{leg} truncated at {MAX_TOKENS} tokens "
                        f"(raise BENCH_MAX_TOKENS)")
    if not turn.text:
        raise Truncated(f"{leg} returned nothing")


def warm_tts() -> str | None:
    """One throwaway synthesis before the clock matters.

    Without it the first measured row absorbs client construction, TLS handshake
    and — if MMS is in the chain — a model load, and reads as a latency finding
    about sentence 1 rather than a cold start.
    """
    try:
        _, provider, _ = timed_synth("சரி", "ta")
        return provider
    except Exception as e:  # noqa: BLE001 — a warm-up failure is not a run failure
        print(f"  (TTS warm-up failed: {e})")
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-tts", action="store_true", help="skip Sarvam calls")
    args = ap.parse_args()

    if not GROQ_API_KEY:
        print("GROQ_API_KEY not set", file=sys.stderr)
        return 1

    print(f"model={CHAT_MODEL}  speech_rate={WORDS_PER_SEC} w/s\n")

    # ── Part 1: prefix stability ─────────────────────────────────────────────
    print("=" * 72)
    print("PART 1  Prefix stability — can we commit output before the verb lands?")
    print("=" * 72)

    direct, pivot = [], []
    bad_direct = bad_pivot = 0
    trunc_direct = trunc_pivot = 0
    cuts_direct = cuts_pivot = 0
    for i, s in enumerate(SENTENCES, 1):
        d = stability_run(s, "ta")
        p = stability_run(s, "en")
        bad_direct += d.degenerate
        bad_pivot += p.degenerate
        trunc_direct += d.truncated
        trunc_pivot += p.truncated
        cuts_direct += d.n_cuts
        cuts_pivot += p.n_cuts
        direct.append(d.mean)
        pivot.append(p.mean)

        print(f"\n[{i}] {s}")
        for label, run in (("te→ta (direct, SOV→SOV)", d), ("te→en (pivot,  SOV→SVO)", p)):
            print(f"    {label}: {run.mean:.0%}"
                  f"  (scored {len(run.scores)}/{max(run.n_cuts - 1, 0)} transitions"
                  f"{f', {run.degenerate} degenerate' if run.degenerate else ''}"
                  f"{f', {run.truncated} TRUNCATED' if run.truncated else ''})")
            for st in run.steps:
                tag = f"[{st['words']}/{st['of']}w]"
                if "error" in st:
                    print(f"        {tag} <<CALL FAILED>> {st['error']}")
                elif st.get("truncated"):
                    print(f"        {tag} <<TRUNCATED at {MAX_TOKENS} tokens — raise "
                          f"BENCH_MAX_TOKENS>> {st['out']!r}")
                elif st.get("degenerate"):
                    print(f"        {tag} <<DEGENERATE>> {st['out']!r}")
                else:
                    sc = st.get("stability")
                    print(f"        {tag} {st['out']}"
                          + (f"   ({sc:.0%} kept)" if sc is not None else ""))

    md, mp = statistics.mean(direct), statistics.mean(pivot)
    print("\n" + "-" * 72)
    print(f"  MEAN STABILITY   te→ta {md:.0%}   |   te→en {mp:.0%}")
    print(f"  DEGENERATE OUT   te→ta {bad_direct}/{cuts_direct}"
          f"  |   te→en {bad_pivot}/{cuts_pivot}")
    if trunc_direct or trunc_pivot:
        print(f"  TRUNCATED        te→ta {trunc_direct}/{cuts_direct}"
              f"  |   te→en {trunc_pivot}/{cuts_pivot}"
              f"   ⚠ instrument fault, not a result")
        print(f"                   raise BENCH_MAX_TOKENS above {MAX_TOKENS} and re-run;"
              " these steps are excluded")
    print(f"  n = {len(SENTENCES)} sentences — small. Treat as a signal, not a result.")
    print("-" * 72)

    # ── Part 2: time to first audio ──────────────────────────────────────────
    if not args.no_tts:
        print("\n" + "=" * 72)
        print("PART 2  Time to first audio, measured from START of the utterance")
        print("=" * 72)
        warm = warm_tts()
        if warm:
            print(f"  (TTS warmed; provider={warm})")
        providers: set[str] = set()

        rows = []
        for s in SENTENCES:
            words = s.split()
            speak_time = len(words) / WORDS_PER_SEC

            # Route A — pivot, consecutive. What Samsung/Jio ship. You cannot start
            # until the speaker stops, then you pay two LLM hops plus TTS.
            t1 = _chat(_FULL.format(src="Telugu", tgt="English"), s)
            _guard(t1, "te→en")
            t2 = _chat(_FULL.format(src="English", tgt="Tamil"), t1.text)
            _guard(t2, "en→ta")
            t_tts_a, prov_a, _ = timed_synth(t2.text, "ta")
            a = speak_time + t1.seconds + t2.seconds + t_tts_a

            # Route B — direct, consecutive. One hop instead of two.
            t3 = _chat(_FULL.format(src="Telugu", tgt="Tamil"), s)
            _guard(t3, "te→ta")
            t_tts_b, prov_b, _ = timed_synth(t3.text, "ta")
            b = speak_time + t3.seconds + t_tts_b

            # Route C — direct, incremental. Translate at 60% of the utterance and
            # start speaking then. Only legitimate if Part 1 says the prefix holds.
            cut = max(2, int(len(words) * 0.6))
            partial = " ".join(words[:cut])
            head = _chat(_INCREMENTAL.format(src="Telugu", tgt="Tamil"), partial)
            _guard(head, "te→ta partial")
            t_tts_c, prov_c, _ = timed_synth(head.text, "ta")
            c = (cut / WORDS_PER_SEC) + head.seconds + t_tts_c

            providers.update(p for p in (prov_a, prov_b, prov_c) if p)
            rows.append((a, b, c))
            print(f"\n  {s[:50]}...")
            print(f"    A pivot consecutive   {a:6.2f}s "
                  f"(speak {speak_time:.1f} + mt {t1.seconds + t2.seconds:.2f} + tts {t_tts_a:.2f})")
            print(f"    B direct consecutive  {b:6.2f}s "
                  f"(speak {speak_time:.1f} + mt {t3.seconds:.2f} + tts {t_tts_b:.2f})")
            print(f"    C direct incremental  {c:6.2f}s "
                  f"(speak {cut / WORDS_PER_SEC:.1f} + mt {head.seconds:.2f} + tts {t_tts_c:.2f})")

        ma = statistics.mean(r[0] for r in rows)
        mb = statistics.mean(r[1] for r in rows)
        mc = statistics.mean(r[2] for r in rows)
        print("\n" + "-" * 72)
        print(f"  MEAN TTFA   A pivot {ma:.2f}s   B direct {mb:.2f}s   C incremental {mc:.2f}s")
        print(f"  C saves {ma - mc:.2f}s vs the route every shipping product uses")
        if len(providers) > 1:
            print(f"  ⚠  mixed TTS providers across rows ({sorted(providers)}) — "
                  "timings are not comparable")
        print("-" * 72)

    # ── Verdict ──────────────────────────────────────────────────────────────
    # Two separate questions, and conflating them is how you ship a bad product:
    # (a) is direct BETTER than the pivot — the research claim;
    # (b) is direct GOOD ENOUGH to speak before the sentence ends — the product bar.
    # Below ~85% you are contradicting yourself in the listener's ear often enough
    # that they would rather have waited.
    SPEAKABLE = 0.85

    print("\nVERDICT")
    if md > mp + 0.15 and md >= SPEAKABLE:
        print(f"  Thesis HOLDS and clears the product bar. Direct is {md - mp:.0%} more")
        print(f"  stable than the pivot, at {md:.0%} absolute (≥{SPEAKABLE:.0%}).")
        print("  → Build Phase 2 (incremental translation) on the direct route.")
    elif md > mp + 0.15:
        print(f"  Thesis holds DIRECTIONALLY: direct is {md - mp:.0%} more stable than the")
        print(f"  English pivot. But {md:.0%} absolute is below the {SPEAKABLE:.0%} needed to")
        print("  commit speech mid-sentence — at this level you would contradict")
        print("  yourself in the listener's ear roughly half the time.")
        print("  → The word-order argument is real. The current model is not good")
        print("     enough to exploit it. Next variable is the MODEL, not the route:")
        print("     try an Indic-tuned model (Sarvam, or IndicTrans2 via Bhashini)")
        print("     before concluding anything about the architecture.")
        if bad_direct > bad_pivot:
            print(f"  ⚠  Direct also failed outright more often ({bad_direct} vs {bad_pivot}")
            print("     degenerate outputs). Reliability, not just stability, is a blocker.")
    elif md > mp:
        print(f"  Thesis WEAK. Direct is only {md - mp:.0%} more stable — real but")
        print("  probably not a defensible advantage on its own.")
        print("  → Re-run with more sentences before committing to the architecture.")
    else:
        print("  Thesis FAILS. Direct is no more stable than the English pivot.")
        print("  → The SOV argument does not survive contact with this model.")
        print("  → Differentiate on telephony + voice quality instead, not latency.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
