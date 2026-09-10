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

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

import argparse
import json
import os
import statistics
import sys
import time

import httpx


# Below this you are contradicting yourself in the listener's ear often enough
# that they would rather have waited for you to finish. It is the product bar,
# and it is a separate question from whether direct beats the pivot.
SPEAKABLE = 0.85

LANG = {"te": "Telugu", "ta": "Tamil", "en": "English"}
LANG_CODE = {"te": "te-IN", "ta": "ta-IN", "en": "en-IN"}


# Conversational speech runs ~2.5-3 words/sec. Telugu sits at the low end because
# the words are longer. This is what makes the streaming simulation honest: a
# 12-word sentence takes ~4.3s to say, and that is the budget a real simultaneous
# translator is spending against.
WORDS_PER_SEC = 2.8

# Groq's free tier rate-limits on burst. Spacing calls keeps the sample complete
# rather than truncated at whatever point the 429 lands.
PACE_SLEEP = float(os.environ.get("BENCH_SLEEP", "1.5"))

# ─────────────────────────────────────────────────────────────────────────────
# Backends
#
# The whole point of the flag: hold the sentences, prompts, cut points and
# scoring fixed and swap only the translator. Anything that differs between two
# runs other than the model is a confound, so the backends deliberately share
# one request path and one retry policy.
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Backend:
    name: str
    kind: str          # "chat" = OpenAI-compatible /chat/completions; "mt" = Sarvam /translate
    base: str
    key_env: str
    model: str
    note: str
    # An MT endpoint takes text, not instructions. It cannot be told "you are
    # seeing a fragment, never revise" — so its stability score is raw MT
    # stability, which is a fair thing to measure and a different thing to
    # measure. Flagged rather than hidden.
    honors_incremental: bool = True


BACKENDS: dict[str, Backend] = {
    # NB: llama-3.3-70b-versatile — the default in agent.py:144 — has been
    # decommissioned by Groq and now 404s. Benchmarked against a live model.
    "groq": Backend(
        name="groq",
        kind="chat",
        base=os.environ.get("GROQ_BASE", "https://api.groq.com/openai/v1"),
        key_env="GROQ_API_KEY",
        model=os.environ.get("BENCH_MODEL", "openai/gpt-oss-120b"),
        note="general-purpose, no Indic specialisation — the control",
    ),
    # sarvam-m is deprecated and 400s; sarvam-105b is the live model. Same
    # OpenAI-compatible shape as Groq, so the prompts carry over verbatim.
    "sarvam": Backend(
        name="sarvam",
        kind="chat",
        base="https://api.sarvam.ai/v1",
        key_env="SARVAM_API_KEY",
        model=os.environ.get("BENCH_SARVAM_MODEL", "sarvam-105b"),
        note="Indic-tuned LLM, identical prompts — the experiment",
    ),
    "sarvam-translate": Backend(
        name="sarvam-translate",
        kind="mt",
        base="https://api.sarvam.ai",
        key_env="SARVAM_API_KEY",
        model=os.environ.get("BENCH_SARVAM_MT_MODEL", "sarvam-translate:v1"),
        note="dedicated Indic MT — cannot be instructed, measures raw MT stability",
        honors_incremental=False,
    ),
}


# Real business-call utterances, all verb-final, all the shape this product would
# actually see. Held in Telugu script the way Whisper returns it, English
# loanwords included, because that is how the language is actually spoken on a
# sales call and stripping them would be benchmarking a sentence nobody says.
#
# The first ten are the original sample the funding pitch quotes; --limit 10
# reproduces those numbers exactly. The rest exist because n=10 is too small to
# conclude anything from, which the first run said in its own output.
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
    "ఆ apartment లో lift మరియు generator backup రెండూ ఉన్నాయి సార్",
    "మీకు వీలైతే ఈ weekend లో ఒకసారి office కి వచ్చి కలవండి",
    "ఆ plot కి clear title ఉంది మరియు అన్ని documents ready గా ఉన్నాయి",
    "నేను మీకు WhatsApp లో floor plan మరియు price sheet పంపిస్తాను",
    "ఈ price లో negotiation కి ఇంకా కొంచెం scope ఉందని owner చెప్పారు",
    "మీరు అడిగిన two BHK ఆ building లో ఇప్పుడు ఖాళీగా లేదు అండి",
    "maintenance charges నెలకు square feet కి మూడు rupees అవుతుంది",
    "ఆ colony లో school మరియు hospital రెండూ నడక దూరంలో ఉన్నాయి",
    "loan కోసం మీ salary slips మరియు bank statement కావాలి సార్",
    "ఈ deal ఈ నెల లోపు finalize చేస్తే discount ఇస్తామని అన్నారు",
    "ఆ property మీద ఇప్పటికే ఒక booking amount pay అయిపోయింది",
    "మీ family తో కలిసి ఒకసారి site చూసి decision తీసుకోండి",
    "రేపు మధ్యాహ్నం రెండు గంటలకు నేను మీకు call చేస్తాను సార్",
    "ఆ tower లో east facing flats అన్నీ already sold out అయ్యాయి",
    "registration అయిన వెంటనే keys మీ చేతికి ఇచ్చేస్తాము అండి",
    "ఈ builder గత పది సంవత్సరాలలో పన్నెండు projects పూర్తి చేశారు",
    "మీరు cash payment చేస్తే GST మీద కొంత తగ్గింపు వస్తుంది",
    "ఆ flat కి car parking slot ఒకటి free గా included ఉంది",
    "మీ budget చెబితే నేను దానికి సరిపోయే options వెతికి పెడతాను",
    "ఆ area లో metro station వచ్చే సంవత్సరం లోపు పూర్తి అవుతుంది",
]


def translate(
    be: Backend, src: str, tgt: str, text: str, *, incremental: bool
) -> Turn:
    """One translation.

    Elapsed is wall-clock request time only — the pacing sleep is taken after the
    clock stops, so the latency numbers are not inflated by our own rate limiting.
    """
    key = os.environ.get(be.key_env, "")
    t0 = time.perf_counter()

    if be.kind == "mt":
        data = _post(
            f"{be.base}/translate",
            {"api-subscription-key": key, "Content-Type": "application/json"},
            {
                "input": text,
                "source_language_code": LANG_CODE[src],
                "target_language_code": LANG_CODE[tgt],
                "model": be.model,
            },
        )
        out = (data.get("translated_text") or "").strip()
        truncated = False
    else:
        sysmsg = (_INCREMENTAL if incremental else _FULL).format(
            src=LANG[src], tgt=LANG[tgt]
        )
        data = _post(
            f"{be.base}/chat/completions",
            {"Authorization": f"Bearer {key}"},
            {
                "model": be.model,
                "messages": [
                    {"role": "system", "content": sysmsg},
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


def stability_run(be: Backend, sentence: str, tgt: str, *, steps: int = 5) -> StabilityRun:
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
        try:
            turn = translate(be, "te", tgt, frag, incremental=True)
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
    ap = argparse.ArgumentParser(
        description="Prefix stability and time-to-first-audio for direct vs pivoted translation."
    )
    ap.add_argument("--backend", default="groq", choices=sorted(BACKENDS),
                    help="which translator to measure (default: groq)")
    ap.add_argument("--model", help="override the backend's model id")
    ap.add_argument("--limit", type=int, default=0,
                    help="use only the first N sentences (0 = all; 10 = the original sample)")
    ap.add_argument("--steps", type=int, default=5,
                    help="prefix cut points per sentence (default 5)")
    ap.add_argument("--no-tts", action="store_true", help="skip Part 2 / all TTS calls")
    ap.add_argument("--json", dest="json_path", help="write raw results here")
    args = ap.parse_args()

    be = BACKENDS[args.backend]
    if args.model:
        be = replace(be, model=args.model)

    if not os.environ.get(be.key_env):
        print(f"{be.key_env} not set (required for --backend {be.name})", file=sys.stderr)
        return 1

    corpus = SENTENCES[: args.limit] if args.limit > 0 else SENTENCES

    print(f"backend={be.name}  model={be.model}  ({be.note})")
    print(f"n={len(corpus)} sentences  steps={args.steps}  speech_rate={WORDS_PER_SEC} w/s")
    if not be.honors_incremental:
        print("NOTE: this backend takes text, not instructions. It cannot be told")
        print("      'never revise', so Part 1 measures raw MT stability.")
    print()

    results: dict = {
        "started_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "backend": be.name,
        "model": be.model,
        "honors_incremental": be.honors_incremental,
        "n_sentences": len(corpus),
        "steps": args.steps,
        "words_per_sec": WORDS_PER_SEC,
        "sentences": [],
    }

    # ── Part 1: prefix stability ─────────────────────────────────────────────
    print("=" * 72)
    print("PART 1  Prefix stability — can we commit output before the verb lands?")
    print("=" * 72)

    direct, pivot = [], []
    bad_direct = bad_pivot = 0
    trunc_direct = trunc_pivot = 0
    cuts_direct = cuts_pivot = 0
    wins = ties = losses = 0
    for i, s in enumerate(corpus, 1):
        d = stability_run(be, s, "ta", steps=args.steps)
        p = stability_run(be, s, "en", steps=args.steps)
        bad_direct += d.degenerate
        bad_pivot += p.degenerate
        trunc_direct += d.truncated
        trunc_pivot += p.truncated
        cuts_direct += d.n_cuts
        cuts_pivot += p.n_cuts
        direct.append(d.mean)
        pivot.append(p.mean)

        # A mean of means hides the shape. Per-sentence outcomes say whether one
        # route wins broadly or wins one sentence hugely, which are different
        # claims and only the first is defensible.
        if d.mean - p.mean > 0.05:
            wins += 1
        elif p.mean - d.mean > 0.05:
            losses += 1
        else:
            ties += 1

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
        results["sentences"].append({
            "sentence": s,
            "direct": d.__dict__,
            "pivot": p.__dict__,
        })

    md, mp = statistics.mean(direct), statistics.mean(pivot)
    med_d, med_p = statistics.median(direct), statistics.median(pivot)
    print("\n" + "-" * 72)
    print(f"  MEAN STABILITY   te→ta {md:.0%}   |   te→en {mp:.0%}")
    print(f"  MEDIAN           te→ta {med_d:.0%}   |   te→en {med_p:.0%}")
    print(f"  DEGENERATE OUT   te→ta {bad_direct}/{cuts_direct}"
          f"  |   te→en {bad_pivot}/{cuts_pivot}")
    if trunc_direct or trunc_pivot:
        print(f"  TRUNCATED        te→ta {trunc_direct}/{cuts_direct}"
              f"  |   te→en {trunc_pivot}/{cuts_pivot}"
              f"   ⚠ instrument fault, not a result")
        print(f"                   raise BENCH_MAX_TOKENS above {MAX_TOKENS} and re-run;"
              " these steps are excluded")
    print(f"  PER-SENTENCE     direct wins {wins}, ties {ties}, pivot wins {losses}"
          f"  (of {len(corpus)})")
    if len(corpus) < 20:
        print(f"  n = {len(corpus)} sentences — small. Treat as a signal, not a result.")
    print("-" * 72)

    results["part1"] = {
        "mean_direct": md, "mean_pivot": mp,
        "median_direct": med_d, "median_pivot": med_p,
        "degenerate_direct": bad_direct, "degenerate_pivot": bad_pivot,
        "truncated_direct": trunc_direct, "truncated_pivot": trunc_pivot,
        "max_tokens": MAX_TOKENS,
        "cuts_direct": cuts_direct, "cuts_pivot": cuts_pivot,
        "sentence_wins_direct": wins, "ties": ties, "sentence_wins_pivot": losses,
    }

    # ── Part 2: time to first audio ──────────────────────────────────────────
    ma = mb = mc = None
    save_ab = save_ac = None
    if not args.no_tts:
        print("\n" + "=" * 72)
        print("PART 2  Time to first audio, measured from START of the utterance")
        print("=" * 72)
        warm = warm_tts()
        if warm:
            print(f"  (TTS warmed; provider={warm})")
        providers: set[str] = set()

        rows: list[dict] = []
        for s in corpus:
            words = s.split()
            speak_time = len(words) / WORDS_PER_SEC
            row: dict = {"sentence": s, "speak_s": speak_time,
                         "a": None, "b": None, "c": None, "errors": {}}
            print(f"\n  {s[:50]}...")

            # Each route is measured independently. Wrapping the whole sentence
            # in one try — as the first version of this did — meant a single
            # flaky TTS call threw away the two routes that had already
            # succeeded, and a run that loses rows unevenly across routes is
            # comparing different sentences to each other.

            # Route A — pivot, consecutive. What Samsung/Jio ship. You cannot
            # start until the speaker stops, then you pay two hops plus TTS.
            try:
                t1 = translate(be, "te", "en", s, incremental=False)
                _guard(t1, "te→en")
                t2 = translate(be, "en", "ta", t1.text, incremental=False)
                _guard(t2, "en→ta")
                mt = t1.seconds + t2.seconds
                t_tts, prov, _ = timed_synth(t2.text, "ta")
                row["a"] = speak_time + mt + t_tts
                providers.add(prov)
                print(f"    A pivot consecutive   {row['a']:6.2f}s "
                      f"(speak {speak_time:.1f} + mt {mt:.2f} + tts {t_tts:.2f})")
            except (CallFailed, RuntimeError) as e:
                row["errors"]["a"] = str(e)
                print(f"    A pivot consecutive      n/a  ({e})")

            # Route B — direct, consecutive. One hop instead of two.
            try:
                t1 = translate(be, "te", "ta", s, incremental=False)
                _guard(t1, "te→ta")
                t_tts, prov, _ = timed_synth(t1.text, "ta")
                row["b"] = speak_time + t1.seconds + t_tts
                providers.add(prov)
                print(f"    B direct consecutive  {row['b']:6.2f}s "
                      f"(speak {speak_time:.1f} + mt {t1.seconds:.2f} + tts {t_tts:.2f})")
            except (CallFailed, RuntimeError) as e:
                row["errors"]["b"] = str(e)
                print(f"    B direct consecutive     n/a  ({e})")

            # Route C — direct, incremental. Translate at 60% of the utterance
            # and start speaking then. Only a legitimate number if Part 1 says
            # the prefix holds; the verdict below refuses to credit it otherwise.
            cut = max(2, int(len(words) * 0.6))
            try:
                partial = " ".join(words[:cut])
                head = translate(be, "te", "ta", partial, incremental=True)
                _guard(head, "te→ta partial")
                if _is_degenerate(head.text):
                    # No audio to play means no time to first audio. Scoring this
                    # as speak+mt+0 would credit the route for having failed.
                    row["errors"]["c"] = f"degenerate partial: {head.text!r}"
                    print(f"    C direct incremental     n/a  "
                          f"(degenerate partial: {head.text!r})")
                else:
                    t_tts, prov, _ = timed_synth(head.text, "ta")
                    row["c"] = (cut / WORDS_PER_SEC) + head.seconds + t_tts
                    providers.add(prov)
                    print(f"    C direct incremental  {row['c']:6.2f}s "
                          f"(speak {cut / WORDS_PER_SEC:.1f} + mt {head.seconds:.2f} "
                          f"+ tts {t_tts:.2f})")
            except (CallFailed, RuntimeError) as e:
                row["errors"]["c"] = str(e)
                print(f"    C direct incremental     n/a  ({e})")

            rows.append(row)

        got_a = [r["a"] for r in rows if r["a"] is not None]
        got_b = [r["b"] for r in rows if r["b"] is not None]
        got_c = [r["c"] for r in rows if r["c"] is not None]
        if got_a or got_b:
            ma = statistics.mean(got_a) if got_a else None
            mb = statistics.mean(got_b) if got_b else None
            mc = statistics.mean(got_c) if got_c else None

            # Savings are computed PAIRED — only over sentences where both
            # routes produced audio. Differencing two means taken over
            # different sentence sets is an artefact of which calls happened to
            # fail, not a latency finding.
            pair_ab = [(r["a"], r["b"]) for r in rows
                       if r["a"] is not None and r["b"] is not None]
            pair_ac = [(r["a"], r["c"]) for r in rows
                       if r["a"] is not None and r["c"] is not None]
            save_ab = statistics.mean(a - b for a, b in pair_ab) if pair_ab else None
            save_ac = statistics.mean(a - c for a, c in pair_ac) if pair_ac else None

            def _s(x: float | None) -> str:
                return "n/a" if x is None else f"{x:.2f}s"

            print("\n" + "-" * 72)
            print(f"  MEAN TTFA   A pivot {_s(ma)} (n={len(got_a)})   "
                  f"B direct {_s(mb)} (n={len(got_b)})   "
                  f"C incremental {_s(mc)} (n={len(got_c)})")
            if save_ab is not None:
                print(f"  B saves {save_ab:.2f}s just by dropping the English hop"
                      f"  (paired, n={len(pair_ab)})")
            if save_ac is not None:
                print(f"  C saves {save_ac:.2f}s vs the route every shipping product uses"
                      f"  (paired, n={len(pair_ac)} of {len(rows)} sentences)")
            if len(got_c) < len(rows):
                print(f"  ⚠  Route C produced no speakable audio on "
                      f"{len(rows) - len(got_c)}/{len(rows)} sentences — a route that")
                print("     stays silent is not a fast route.")
            if len(providers) > 1:
                print(f"  ⚠  mixed TTS providers across rows ({sorted(providers)}) — "
                      "timings are not comparable")
            print("-" * 72)
            results["part2"] = {
                "mean_ttfa_pivot": ma, "mean_ttfa_direct": mb,
                "mean_ttfa_incremental": mc,
                "paired_saving_direct_vs_pivot": save_ab,
                "paired_saving_incremental_vs_pivot": save_ac,
                "n_a": len(got_a), "n_b": len(got_b), "n_c": len(got_c),
                "n_sentences": len(rows),
                "tts_providers": sorted(providers),
                "rows": rows,
            }

    # ── Verdict ──────────────────────────────────────────────────────────────
    # Two separate questions, and conflating them is how you ship a bad product:
    # (a) is direct BETTER than the pivot — the research claim;
    # (b) is direct GOOD ENOUGH to speak before the sentence ends — the product bar.
    print("\nVERDICT")
    gap = md - mp
    if gap > 0.15 and md >= SPEAKABLE:
        verdict = "holds_and_speakable"
        print(f"  Thesis HOLDS and clears the product bar. Direct is {gap:.0%} more")
        print(f"  stable than the pivot, at {md:.0%} absolute (≥{SPEAKABLE:.0%}).")
        print("  → Build Phase 2 (incremental translation) on the direct route.")
    elif gap > 0.15:
        verdict = "holds_directionally"
        print(f"  Thesis holds DIRECTIONALLY: direct is {gap:.0%} more stable than the")
        print(f"  English pivot, and wins {wins} of {len(corpus)} sentences outright.")
        print(f"  But {md:.0%} absolute is below the {SPEAKABLE:.0%} needed to commit speech")
        print("  mid-sentence — at this level you would contradict yourself in the")
        print("  listener's ear too often to be worth the head start.")
        print("  → The word-order argument is real. This model cannot exploit it.")
        if be.name == "groq":
            print("  → Next variable is the MODEL, not the route. Re-run this exact")
            print("     command with --backend sarvam (Indic-tuned, same prompts)")
            print("     before concluding anything about the architecture.")
        else:
            print(f"  → Already on {be.name}. If an Indic-tuned model still sits below")
            print(f"     {SPEAKABLE:.0%}, the constraint is likelier the task than the model:")
            print("      re-scope Phase 2 to phrase-level commitment, or ship Phase 1.")
        if bad_direct > bad_pivot:
            print(f"  ⚠  Direct also failed outright more often ({bad_direct} vs {bad_pivot}")
            print("     degenerate outputs). Reliability, not just stability, is a blocker.")
    elif gap > 0.05:
        verdict = "weak"
        print(f"  Thesis WEAK. Direct is only {gap:.0%} more stable — real but")
        print("  probably not a defensible advantage on its own.")
        print(f"  (direct wins {wins}, ties {ties}, pivot wins {losses})")
        print("  → Widen the corpus or change the model before committing.")
    else:
        verdict = "fails"
        print("  Thesis FAILS. Direct is no more stable than the English pivot.")
        print("  → The SOV argument does not survive contact with this model.")
        print("  → Differentiate on telephony + voice quality instead, not latency.")

    # Dropping the English hop is a real latency win regardless of how the
    # stability question lands, and it needs no incremental machinery at all.
    if save_ab is not None and save_ab > 0:
        print(f"\n  Independent of all that: the direct route is {save_ab:.2f}s faster")
        print("  end-to-end than the pivot on consecutive translation alone, which")
        print("  Phase 1 already gets for free. That part is not contingent.")
    if mc is not None and md < SPEAKABLE:
        print(f"\n  The {mc:.2f}s Route C number is a ceiling, not a promise: at {md:.0%}")
        print("  stability that audio is sometimes wrong. Do not quote it as a")
        print("  product latency until stability clears the bar.")

    results["verdict"] = verdict
    results["speakable_threshold"] = SPEAKABLE

    out_path = args.json_path or f"bench_{be.name}_{len(corpus)}s.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\nraw results → {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
