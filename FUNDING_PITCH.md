# Setu — every Indian language on the same phone call

> **Working name.** *Setu* = bridge. Placeholder until we check trademark availability.

**One line:** Two people pick up the phone, each speaks only their own Indian language,
and each hears the other in theirs — over a normal phone call, no app, no smartphone
required on either end.

**Status:** we are not starting from zero. A multi-tenant voice agent already answers
real phone calls in Telugu and Hinglish, in production, with call recording, lead
extraction and a client dashboard. This proposal turns that engine from *one AI talking
to one human* into *two humans talking to each other*.

---

## 1. The problem, stated concretely

India has 22 scheduled languages, 121 languages with more than 10,000 speakers, and
1,369 mother tongues in the census. English is spoken by roughly 10% of the population,
and Hindi is not a solution in the South.

So every day, across the country, transactions fail on language:

- A Telugu-speaking builder's sales desk in Hyderabad gets a call from a Marathi-speaking
  buyer in Pune. Both fall back to broken English. The nuance — and often the deal — is lost.
- A migrant worker from Odisha is admitted to a Hyderabad hospital. The intake nurse
  speaks Telugu. Consent, allergies and symptom history are taken through whoever in the
  corridor speaks both.
- A Tamil Nadu logistics operator negotiates with a Gujarati transporter over WhatsApp
  voice notes, each running them through a translation app separately, one direction at a time.

The workaround today is a bilingual human — a colleague, a relative, a hired interpreter.
That person is expensive, unavailable at 9pm, and often the reason the conversation
didn't happen at all.

---

## 2. Why this doesn't exist yet

This is the question the panel will ask, and the honest answer is the strongest part of
the pitch. **It is not because nobody thought of it.** Five real reasons:

### 2.1 The big players solved the wrong pair
Google Meet's speech translation went GA in early 2026 with five language pairs, added
Hindi around August 2026 — **six pairs, every one anchored on English**, one active at a
time. Microsoft Teams' Interpreter agent covers 10 languages, no Indian language among
them. DeepL launched Voice-to-Voice in April 2026 across 40+ languages; of India's 22,
it has Bengali. Samsung Live Translate does real phone calls on-device — and its call
language list has Hindi and English, **no Telugu, no Tamil, no Bengali, no Marathi**.

Every one of them treats Indian languages as a long-tail localisation problem to be
reached eventually, from English outward. Nobody has built Indic-to-Indic as the
*primary* case.

### 2.2 Everything routes through English, and that costs latency
This is the technical crux. Telugu, Tamil, Hindi, Kannada, Bengali and Marathi are all
**verb-final (SOV)**. English is **SVO**. Translating Telugu→English means you cannot
emit the English sentence until the Telugu verb arrives — and it arrives last. That wait
is the single largest source of the 2–4 second delay reviewers complain about.

Telugu→Tamil does not have that problem. Word order aligns. In principle you can
translate incrementally and start speaking before the speaker finishes.

**Every shipping product pays the English word-order penalty on Indic pairs, for no
reason other than that English sits in the middle of their pipeline.**

**We measured it.** `bench_translate.py` feeds a translator growing prefixes of a Telugu
sentence and scores how much of what it already emitted survives unchanged as more words
arrive — *prefix stability*. High stability means you could safely have spoken it already.

| Route | Mean prefix stability | Degenerate outputs |
|---|---|---|
| **te→ta direct** (SOV→SOV) | **53%** | 12 / 50 |
| te→en pivot (SOV→SVO) | 28% | 6 / 50 |

n = 10 conversational sentences, `openai/gpt-oss-120b`, temperature 0. Direct wins 7 of
10 outright, 2 near-ties, 1 loss.

**Direct Indic→Indic is ~1.9× more prefix-stable than routing through English.** The
word-order argument survives contact with data.

> **What this does not yet show, stated plainly:** 53% absolute is *below the ~85% needed
> to actually start speaking mid-sentence* — at this level you would contradict yourself
> in the listener's ear about half the time. And the direct route failed outright twice as
> often (12 vs 6 empty or truncated outputs). So the *direction* is proven; the *level* is
> not there yet.
>
> The most likely cause is the model: `gpt-oss-120b` has no Indic specialisation. The next
> experiment — and a specific thing this funding buys — is re-running against an
> Indic-tuned model (Sarvam's own, or IndicTrans2, which Bhashini serves free) before
> drawing any conclusion about the architecture.

### 2.3 The market that exists is priced for events, not phone calls
KUDO and Interprefy charge roughly ₹50–105 per minute, because they are priced against
human conference interpreters. That is fine for a UN session and absurd for a builder's
sales desk. Nobody has built for the ₹5–20/minute band where Indian SMB volume actually lives.

### 2.4 It is genuinely hard, and the shipped versions aren't good enough to keep
The existing products are **consecutive, not simultaneous** — you speak, you stop, you
wait, it speaks. Every call takes twice as long. Reviewers report Samsung fumbling a word
or a name at least once per call, and an awkward pause right after "Hello?" There is 22%
user dissatisfaction on complex financial and legal calls. The industry's own line is:
*if the call flow feels clumsy, adoption drops fast*.

**So the market isn't empty. It's full of things people try twice and abandon.** That is
a harder problem than an empty market — but it's also why the incumbents' head start is
worth less than it looks.

### 2.5 The voices don't exist for most languages
Machines can now *transcribe* ~1,600 languages (Meta's Omnilingual ASR, extending to
5,400 zero-shot). They can *speak* naturally in about 35. The bottleneck is text-to-speech,
which needs clean paired studio recordings that mostly don't exist and can't be
economically made. **Understanding is nearly solved; speaking is not.**

This is exactly why a working Sarvam Bulbul v3 integration with per-language tier-1
speakers — which we already have — is a real asset rather than a commodity.

---

## 3. The gap, precisely bounded

We are **not** claiming nobody translates calls. Jio, Samsung, Gnani.ai, Reverie, Skit.ai
and Bhashini all exist and are named honestly in §7. The unserved intersection is narrower
and sharper:

| Dimension | Served today by | Gap |
|---|---|---|
| Indic ↔ English | Jio, Samsung, Google, Bhashini | **Crowded. Avoid.** |
| **Indic ↔ Indic, direct, no English pivot** | essentially nobody | **Our wedge** |
| Over WebRTC / meetings | DeepL, Google, Teams, Interprefy | Crowded |
| **Over PSTN, business inbound/outbound** | Jio (consumer P2P only) | **Open** |
| **Multi-party, each hearing their own language** | Google Meet does one pair at a time | **Open, structural** |
| Consumer, free | Jio ₹99→free, Samsung bundled | **Unwinnable. Do not enter.** |
| **B2B, per-seat or per-minute** | interpretation vendors at ₹50–105/min | **Open at ₹5–20/min** |

**The wedge in one sentence:** *business phone calls between two Indian languages,
neither of which is English, priced for an Indian SMB.*

---

## 4. Why us — the unfair advantages

These are not aspirations. They are in the repository today.

| Asset | Where | Why it's hard to copy |
|---|---|---|
| Live SIP/PSTN telephony on LiveKit | `sip-inbound-trunk.json`, `livekit.yaml` | Every meeting vendor is WebRTC-only. Real phone numbers need KYC, DLT compliance, and a working SIP bridge. |
| Sarvam Bulbul v3, tier-1 speaker per language | `tts_engine.py:71` | The scarce asset in the whole field (§2.5). Speaker chosen on character error rate per language, not one favourite reused. |
| Indian number & currency normalisation | `tts_engine.py:162` | `₹50 लाख` → `50 लाख रुपये`, not `50 रुपये लाख`. Phone numbers spoken digit-by-digit. **No global vendor handles this**, and it is in every single business call. |
| Semantic turn detection over Silero VAD | `agent.py` | The exact failure mode that kills Samsung's feature. Already tuned so a drawn breath isn't end-of-turn and an "achha" isn't an interruption. |
| Whisper language hints + bias prompt + echo rejection | `agent.py:442–512` | Code-switched Hinglish handling that generic S2ST does not have. |
| Multi-tenant, auth, recording, dashboards | `tenants.py`, `auth.py`, `recorder.py` | The boring 60% of a B2B product, already done. |
| Sub-₹1/min cost base | `COSTS.md` | Documented vendor-by-vendor, with free tiers mapped. |

**Founder fit:** based in Hyderabad, native Telugu, already shipped and operated a
production Telugu/Hinglish voice product with real callers.

---

## 5. How we win

### Sequence, not a big bang

**Phase 0 — settle the technical bet (weeks 1–2).**
Finish `bench_translate.py`. Prove or kill the SOV→SOV incremental-translation hypothesis
on a proper sample. If it holds, it is a defensible latency advantage on every Indic pair.
If it fails, we still have §4 and we compete on telephony + voice quality + price, and we
find that out for the cost of two weeks instead of two years.

**Phase 1 — two-party translated call (weeks 3–8).**
Two SIP legs into one LiveKit room. One translation agent per direction, each publishing
its own audio track. Reuses the existing STT, TTS, turn detection and recording wholesale.
Target: Telugu ↔ Hindi and Telugu ↔ Tamil first.

**Phase 2 — incremental translation (weeks 9–16).**
Stop waiting for end-of-utterance. Translate committed prefixes, begin TTS on the first
stable phrase. **This is the moat**: it converts a consecutive product people abandon into
a simultaneous one they keep. Only viable if Phase 0 holds.

**Phase 3 — N-way (months 5–6).**
One translated track per target language; each participant subscribes to theirs. Google
Meet does one pair at a time — this is a structural gap, not a coverage gap.

### The three defensible positions

1. **Latency on Indic pairs** — if the SOV thesis holds, we are structurally faster than
   anyone who pivots through English, and they cannot fix it without re-architecting.
2. **Telephony** — a feature phone in a mandi can join. No app, no smartphone, no data.
   This is a large fraction of India and it is invisible to every WebRTC competitor.
3. **The unglamorous Indian details** — lakh/crore, digit-by-digit phone numbers,
   code-switching, honorifics (*అండి*, *जी*). Individually small, collectively the
   difference between a demo and a product.

### Distribution, honestly

We cannot outspend anyone. We win the first 20 customers by hand, in Hyderabad, in person.
Our existing real-estate voice-agent clients are the warm list: they already field
out-of-state buyer calls and already pay us.

---

## 6. Revenue model & unit economics

### Costs per translated minute (estimated, from `COSTS.md` + 2× for bidirectional)

A translated minute costs roughly **double** a normal agent minute — both directions need
STT, translation and TTS.

| Component | Per minute (est.) | Notes |
|---|---|---|
| STT × 2 speakers | ₹0.11 – 0.60 | Groq Whisper paid → Deepgram/Sarvam streaming |
| Translation (LLM) × 2 | ₹0.15 – 0.30 | Cheapest part of the stack |
| TTS × 2 (~1,800 chars/min) | ₹2.00 – 3.00 | Sarvam paid; Edge TTS as free fallback |
| Telephony × 2 legs | ₹1.00 – 2.40 | Plivo → Exotel at India scale |
| Compute / LiveKit | ~₹0.10 | Self-hosted; Oracle Always-Free tier |
| **Total COGS** | **≈ ₹3.40 – 6.40 / min** | |

> These are estimates built on the vendor rates in `COSTS.md` (mid-2026, ~₹84/USD).
> They must be re-verified before any figure goes in front of a customer.

### Pricing

| Tier | Price | Gross margin |
|---|---|---|
| Pay-as-you-go | ₹18 / min | ~65–80% |
| SMB plan | ₹4,999/mo, 400 min included, ₹15/min after | ~65% |
| Enterprise / hospital | ₹24,999/mo, 2,500 min, SLA, on-prem option | ~70% |

### The comparison that sells it

| Option | Cost per minute |
|---|---|
| Human interpreter (agency) | ₹33 – 83 |
| KUDO / Interprefy (event tier) | ₹50 – 105 |
| **Setu** | **₹15 – 18** |
| Our COGS | ₹3.40 – 6.40 |

**We are 3–6× cheaper than the incumbent and still hold a ~70% gross margin.** That gap
exists because they price against human interpreters and we price against software.

### Bottom-up market, Hyderabad first

- ~2,000 real-estate sales desks in HYD/Secunderabad with meaningful out-of-state inflow
- ~400 multi-specialty hospitals with inter-state patient intake
- ~1,500 inter-state logistics and trading firms

At 1% penetration of ~3,900 targets = 39 customers × ₹5,000/mo ≈ **₹23.4 lakh ARR** from
one city. That is the realistic 18-month target, not a TAM slide.

Top-down for context: the interpreting market being disrupted is **$15.8B globally**, and
41% of language-service providers already say they are evaluating AI interpreting.

---

## 7. SWOT

### Strengths
- **A working product already taking real phone calls** — not a prototype, not slideware
- Production Indic TTS with per-language tier-1 speakers; the scarce asset in the field
- Real PSTN/SIP telephony — the thing no meeting vendor has
- Indian-specific text handling (lakh/crore, phone numbers) nobody else built
- Turn-taking already tuned for Indian conversational habits
- Documented sub-₹1/min cost base with mapped free tiers
- Founder is native Telugu, in Hyderabad, on top of the target market

### Weaknesses
- **No explicit translation step yet** — the agent speaks a language, it doesn't interpret between two humans
- Currently 1 agent : 1 caller; N-party audio routing is unbuilt
- `detect_lang()` is script-counting only (`tts_engine.py:280`) — no spoken language ID
- `MMSProvider` is CC-BY-NC — cannot be the commercial path to new languages
- Only hi/te/en voices configured and auditioned; Tamil speaker choice unvalidated
- Single student founder, no sales function, no capital
- **Vendor fragility, already demonstrated:** our default `LLM_MODEL` (`llama-3.3-70b-versatile`,
  `agent.py:144`) was silently decommissioned by Groq and now returns 404. We found it
  during this work. Single-provider dependency is a live risk, not a theoretical one.

### Opportunities
- Indic↔Indic is genuinely unserved by every player named in §2.1
- PSTN is unserved by every meeting vendor
- Bhashini provides free government models across all 22 languages and 1,000+ pre-trained models
- N-way per-language tracks is a structural gap in Google Meet's design
- 3–6× price gap under the incumbent interpretation vendors
- DPDP Act compliance → on-prem/self-hosted deployment is a saleable feature for
  hospitals and government, and one that global SaaS vendors cannot easily match

### Threats
- **Gnani.ai** launched an indigenous voice-to-voice model at the India AI Impact Summit 2026 —
  15+ Indian languages, 200+ enterprise customers including Tata, Mahindra, Air India.
  **This is the most direct competitor and they are ahead on distribution.**
- **Jio** bundles JioTranslate with a SIM 450M+ people already hold, at ₹99/mo and often free
- **Samsung** ships call translation free on the handset; adding Telugu is a config change for them
- **Google** promises Gemini 3.5 Live Translate at 70+ languages and 2,000+ combinations
- **Sarvam** is both our supplier and a potential competitor moving up the stack
- Commoditisation: if end-to-end S2ST models get good, the cascade advantage evaporates
- Model deprecation risk (see Weaknesses — already bit us once)

---

## 8. What would kill this

Stated plainly, because a panel that spots an unacknowledged risk stops believing the rest:

1. **The SOV hypothesis fails** and we have no latency advantage. → Mitigation: Phase 0
   is two weeks and settles it before any real money is spent.
2. **Google adds Telugu↔Tamil** to Meet and Gemini Live. → Mitigation: they still won't
   do PSTN, and won't do lakh/crore. Compete where they structurally aren't.
3. **Gnani gets there first at enterprise scale.** → Mitigation: they sell to Tata and Air
   India; we sell to the 40-person builder. Different buyer, different price point.
4. **Nobody actually pays.** The most likely failure. → Mitigation: Phase 1 ships to
   existing paying clients, not strangers. If our own customers won't pay for it, stop.

---

## 9. The ask

**₹5,00,000 over 12 months**, plus lab/incubation space and a faculty mentor.

| Line item | Amount | Purpose |
|---|---|---|
| Cloud + API credits (Sarvam, Groq/LLM, telephony) | ₹1,20,000 | Move off free tiers; run real pilot traffic |
| Telephony numbers, KYC, DLT registration | ₹60,000 | Legally operate Indian phone numbers |
| GPU compute for incremental-translation R&D | ₹1,00,000 | Phase 0 and Phase 2 experiments |
| Native-speaker evaluation panel (5 languages × 20 hrs) | ₹80,000 | Human quality scoring — the thing no competitor publishes |
| Pilot deployment + travel (Hyderabad) | ₹60,000 | 20 hand-sold customers |
| Legal — entity, trademark, DPDP compliance review | ₹50,000 | |
| Contingency | ₹30,000 | |

### Milestones the funding is judged against

| Month | Deliverable | Pass condition |
|---|---|---|
| 1 | Phase 0 re-run on Indic-tuned models | n ≥ 50 sentences, ≥3 models. Baseline to beat: 53% direct / 28% pivot. Published either way. |
| 3 | Two-party Telugu↔Hindi call live | End-to-end call on real phone numbers |
| 6 | Incremental translation | Time-to-first-audio < 1.5s from utterance start |
| 9 | 5 paying pilot customers | ₹25,000 MRR |
| 12 | 20 customers | ₹1,00,000 MRR, published quality benchmark |

**Note on the benchmark deliverable:** we commit to publishing the numbers *whichever way
they come out*. Two of the four leading global vendors in this space publish no accuracy
metrics at all. Being the ones who do is itself a differentiator.

---

## 10. What already works (demo script)

Lead with the live demo. It is the single biggest advantage over every other pitch in the room.

1. **Call the number from a phone in the room.** Not a video, not a simulation — a real
   inbound PSTN call.
2. Speak Telugu. The agent answers in Telugu, holds a real conversation, handles an
   interruption mid-sentence.
3. Say a price — "*యాభై లక్షలు*". Show it rendered correctly, not as "50 rupees lakh".
4. Hang up. Show the lead already in the Google Sheet and the WhatsApp alert already sent —
   extracted mid-call, before the caller finished.
5. Open the dashboard: recording, transcript, AI summary, tenant isolation.
6. **Then** say: *"Everything you just saw is one AI talking to one human. The proposal is
   to put a second human on the other end, speaking a different Indian language."*

That last line is the pitch. Everything before it is proof you can build it.

---

## 11. Evidence log

| Claim | Status |
|---|---|
| Google Meet: 6 pairs, all English-anchored, one at a time | Verified — Google Workspace Updates, Aug 2026 |
| Samsung call translation excludes Telugu/Tamil/Bengali | Verified — Samsung India support docs |
| DeepL Voice-to-Voice, 40+ languages, Bengali only from India | Verified — DeepL press release, 16 Apr 2026 |
| Gnani.ai indigenous voice-to-voice, 15+ Indic, 200+ enterprises | Verified — India AI Impact Summit 2026 |
| Interpretation market $15.8B; 41% of LSPs evaluating AI | Verified — Slator |
| ASR ~1,600 languages vs natural TTS ~35 | Verified — Meta Omnilingual ASR; SeamlessM4T-v2 |
| Existing products are consecutive; adoption drops on clumsy flow | Verified — Android Authority, PhoneArena reviews |
| SOV→SOV is ~1.9× more prefix-stable than the English pivot | **Measured** — 53% vs 28%, n=10, `bench_translate.py` |
| That stability is high enough to speak mid-sentence today | **NO** — 53% vs ~85% needed. Open problem. |
| Direct route is as *reliable* as the pivot | **NO** — 12/50 vs 6/50 degenerate outputs |
| Unit economics ₹3.40–6.40/min COGS | Estimated from `COSTS.md`, not yet measured on live traffic |

---

*Prepared for the MLRIT innovation funding panel. Figures marked "estimated" are derived
from published vendor rates and must be re-verified before external use.*
