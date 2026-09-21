# ReelForge

Vertical countdown reels — "Top 10 X" — where the footage under each item is
**verifiably of that item**, not something that merely looks similar.

Forked from the `AI_News` reel pipeline. The renderer, the karaoke captions, the
beat alignment and the local voice clone came across unchanged; the input side
(RSS → news story) was replaced with a topic planner, and a sourcing ladder was
added that is the reason this project exists.

```bash
./reelforge.py topics                    # ideas, ranked by how findable footage is
./reelforge.py plan "Top 10 cities to visit before you die"
./reelforge.py build "Top 10 cities to visit before you die"
./reelforge.py make  "..."               # both, in one go
./reelforge.py probe                     # environment check, no network
```

`plan` writes `output/<slug>/plan.json`. Edit it freely — `build` reads the file,
so correcting a fact or swapping an item costs no Gemini quota.

Your own list instead of a generated one:

```bash
./reelforge.py plan "Top 10 richest people" --items my-items.txt
```

## The problem this solves

Stock libraries never return an empty result set. Asked for "Elon Musk", Pexels
reports 1,326 results and offers:

| Query | Pexels top result | |
|---|---|---|
| Niagara Falls | `tour-boat-navigating-niagara-falls-rapids` | genuinely Niagara |
| Machu Picchu | `machu-picchu-ancient-inca-ruins-in-clouds` | genuine |
| Kyoto Japan | `a-woman-walking-down-a-narrow-street-in-japan` | Japan, not Kyoto |
| **Elon Musk** | `drone-flying-over-beach-with-people-enjoying-sunset` | **not remotely** |

So any pipeline that takes the top hit will caption a stranger's holiday footage
with a billionaire's name and never notice. Verification has to be a **hard
gate**, not a ranking preference.

## The ladder

Each item is resolved to a real Wikidata entity first, which settles both its
identity and its ambiguity — "Niagara Falls" is a waterfall (Q34221) *and* a city
in Ontario (Q274120), and picking wrong poisons every search after it.

| Tier | How it is proved | Typically |
|---|---|---|
| **EXACT** | a stock clip whose own page slug names the entity, or a photograph **Wikidata itself attaches** to it (P18/P10), given motion by `kenburns.py` | places, landmarks, and — via Commons — people |
| **PROXY** | a real *related* entity when the subject has none: Musk → Tesla, SpaceX. Still true about the item, never dressed up as the subject | private individuals, abstractions |
| **ILLUSTRATIVE** | a shot **you** named in a spec's `pexels_query`; gated on your words, not the item's name | subjects nobody can identify from a photo (a county, a statistic) |
| **GENERIC** | nothing matched. Mood footage, reserved for the hook, tease and close, and **never allowed to carry a numbered item** | should be rare |

Every shot records its tier in `output/<slug>/reel.json`, and the build prints
any item that did not reach EXACT.

### Why people work at all

No stock library licenses footage of named individuals — that is a rights
problem, not a search problem. Wikidata's `P18` is the photograph Wikipedia uses
for that entity: it *is* the subject, settled by editors rather than by a ranking
function. `kenburns.py` gives it a slow push so it reads as a shot rather than a
dead frame.

### Traps found by reading real data

A Commons category means "associated with", not "is". Left unfiltered it offered:

- the **Taylor Swift Education Center** — a building named after her — as her,
  because her own P18 is a PNG and a "photographs are JPEG" rule discarded it
- a **replica of Niagara Falls in a shop window** as Niagara Falls
- three Grokipedia **screenshots** and a German net-worth **chart** as Elon Musk
- **AI-generated deepfake videos** of Musk, sitting in his own category

So: the Wikidata-pinned file always wins and is exempt from the heuristics;
category files must name the subject, be JPEG, and clear `config.COMMONS_DENY`.

## The format, and where it comes from

The numbers in `config.py` are set from published 2026 short-form retention data,
and each one carries a comment saying which finding it came from.

| Setting | Value | Finding |
|---|---|---|
| `TARGET_SECONDS_MIN/MAX` | 42–58s | the 45–60s bracket had the highest engagement rate and ~2× the median views of sub-30s |
| `SHOTS_PER_ITEM` | 2 | a visual change every 1.5–3s holds attention; static past ~4s is where viewers leave |
| hook | pays off inside ~2s | attention span ~2.1s, first 1.7s is the retention window |
| no intro card | — | filling the 3–15s window with preamble costs 30–50% of viewers |
| `COUNT_DOWN` | true | the unrevealed number one **is** the open loop |
| `MIDROLL_TEASE_AT` | 0.55 | plant a fresh open loop every 10–15s or the middle sags |
| `KARAOKE` | true | word-synced captions dominate top performers |
| `CTA_LINE` | asks for a **send** | DM sends are the top-weighted Reels signal in 2026; likes the weakest |

The number and name of every item are spoken by the pipeline, not written by the
model, so the numeral burned on screen and the numeral in the voice can never
disagree.

## Quota

Gemini's free tier allows 20 requests/day. **Every** response is cached under a
hash of the request in `state/plans/`, so rebuilding after a render bug, a
caption tweak or a footage swap costs nothing. Only `--refresh` spends quota.

Wikidata and Commons are free and unmetered but are cached anyway. Pexels allows
200 searches/hour; one search per phrasing per run, cached in-process.

## Licensing

The Pexels licence permits commercial use with no attribution (the creator is
recorded anyway). Commons files are public domain or CC BY / CC BY-SA — the
latter **legally require credit**, so every such file is collected and rendered
on a closing credits card. `CREDITS_CARD=false` turns the card off; if you do
that, put the credits in the post caption instead.

## Layout

| File | Role |
|---|---|
| `reelforge.py` | CLI: topics / plan / build / make / probe |
| `planner.py` | Gemini → countdown script, written to the format spec, cached |
| `entities.py` | name → Wikidata entity, its distinctive words, its proxies |
| `sources/stock.py` | Pexels, behind the verification gate |
| `sources/commons.py` | Wikimedia Commons, filtered hard |
| `visuals.py` | the ladder: item → verified shots + tier |
| `kenburns.py` | still → moving 9:16 shot |
| `build.py` | beats → voice → timings → shots → mp4 |
| `videocomposite.py` | rank cards, karaoke captions, ffmpeg assembly *(forked)* |
| `voiceclone.py`, `reel_worker.py` | Chatterbox TTS + Whisper word timings *(forked)* |

`REEL_VENV` in `.env` points at the news pipeline's `.venv-reel` so the ~3GB
torch/mlx stack is not installed twice.

## Output

```
output/<slug>/plan.json          the script — edit and rebuild freely
output/<slug>/reel.mp4           the reel
output/<slug>/reel.json          manifest: every shot, its tier, its licence
output/<slug>/reel.caption.txt   caption + hashtags + AI disclosure
output/<slug>/reel.srt           subtitles
```

## The narration bug, and why beats are voiced separately

The first version sent the whole script to Chatterbox as one utterance. At 134
words it fell apart, and nothing in the build reported it — the mp4 looked fine.
Transcribing the audio back showed what had actually been said:

```
Number 5, Sydney. Opera House Roof uses over 1 million tiles.
Number 4, Istanbul is stable.                      <- line truncated to nonsense
And number 5, Sydney. Opera House Roof uses ...    <- ENTIRE ITEM SAID TWICE
Number 1, Vanessa.                                 <- "Venice"
Sinks at the one person you take to number 1. Vanessa sits at number 1. ...
```

"Sydney" appears at 23.14s **and again at 28.02s** — genuinely duplicated audio,
not a transcription artefact. The news pipeline this was forked from only fed it
72–86 words, so the degradation never showed there.

`narrate.py` voices one beat at a time, which fixes more than it set out to:

| | one utterance | per beat |
|---|---|---|
| words heard vs script | 160 vs 134 | **104 vs 104** |
| repeated phrases | a whole item, twice | **none** |
| proper nouns | Venice→"Vanessa", Istanbul→"is stable" | **all correct** |
| item durations | #4 1.7s vs #3 6.5s, equal-length lines | **2.5–4.4s, proportional** |
| #1 / CTA | 2.2s starved / 7.4s bloated | **3.2s / 3.3s** |

Because each beat's duration is then **measured** rather than inferred from
Whisper's word timings, beat spans are exact and contiguous by construction.
That removes the drift `tile_spans` was papering over, and a bad take is cheap
to detect (duration far off the word count, or a repeated 4-gram) and cheap to
redo — one beat, not a minute of narration.

Good takes are cached in `output/<slug>/reel_beats/`, keyed by the line plus the
voice settings, so rebuilding after a footage or caption change re-renders the
video without re-voicing a word.

## Known limitations

- **The asset cache never prunes.** `state/assets/` reached ~1GB across a few
  topics. Delete it freely — everything re-downloads.
- **Wikidata throttles bursts.** Running two builds at once will 429 and cost
  items their entity lookup; the ladder retries and backs off adaptively, but
  one build at a time is the reliable way to run it.

### A third bug inherited from AI_News

`align_beats` bounds each beat by its first and last spoken word, leaving the
pause between beats allocated to neither, while `build_segments` concatenates
only span lengths — so the video track runs progressively ahead of the voice
and one tail stretch hides the total. ReelForge fixes this with
`videocomposite.tile_spans`. **The same bug is present in the AI_News reel
pipeline**, which still has the original `build_segments`. It matters far less
there (every clip is topical to one story, so drift reads as an odd cut rather
than a wrong label) but it is the same defect, and the fix ports directly.

## Building from a hand-written spec

No model, no quota — your words are spoken exactly as written:

```bash
./reelforge.py spec bucket-list.json              # plan and build
./reelforge.py spec bucket-list.json --plan-only  # just show the plan
```

```json
{"intro": {"on_screen_text": "...", "voiceover": "...", "pexels_query": "...", "duration_sec": 3.0},
 "items": [{"rank": 10, "place_name": "Salar de Uyuni, Bolivia",
            "on_screen_text": "10. Salar de Uyuni, Bolivia",
            "voiceover": "Number 10. Salar de Uyuni, Bolivia — the world's largest mirror.",
            "pexels_query": "Salar de Uyuni salt flats", "duration_sec": 2.5}],
 "outro": {"on_screen_text": "...", "voiceover": "...", "duration_sec": 3.0}}
```

Three things the loader does to your file, all deliberate:

- **`voiceover` is spoken verbatim.** Your lines already say "Number 10.", so the
  pipeline does not announce the number again and does not rewrite you to fit a
  word budget.
- **A leading rank is stripped from `on_screen_text`** for the card, because the
  card already draws a large `NO. 10` above the name. `"10. Santorini, Greece"`
  becomes `SANTORINI, GREECE` under the numeral, instead of showing 10 twice.
- **`duration_sec` is a floor, not a cut.** Speech cannot be truncated to fit
  without clipping a word, so a beat runs as long as its line takes; a short one
  is padded up to your figure. Where the two disagree, the build says so.

`pexels_query` changes **what is searched, not what is accepted** — the result
still has to name the entity, so a hand-written query cannot smuggle in footage
of something else.
