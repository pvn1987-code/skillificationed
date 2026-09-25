# ReelForge — operations runbook

Countdown reels ("Top 10 X") where the footage under each item is verifiably of
that item. Forked from the `AI_News` reel pipeline on 2026-09-11.

Nothing here publishes anything. Everything lands in `output/<slug>/`.

---

## 1. Running it

Two entry points, depending on who writes the script.

```bash
# a model writes the list, the facts and the lines
./reelforge.py topics                    # ideas, ranked by footage risk
./reelforge.py plan  "Top 10 cities to visit before you die"
./reelforge.py build "Top 10 cities to visit before you die"
./reelforge.py make  "Top 10 cities to visit before you die"   # plan + build

# you write everything; no model, no quota
./reelforge.py spec bucket-list.json
./reelforge.py spec bucket-list.json --plan-only

./reelforge.py probe                     # environment check, no network
```

`plan` writes `output/<slug>/plan.json` and stops. **Edit that file freely** —
`build` reads it, so correcting a fact, reordering, or swapping an item costs no
Gemini quota.

Your own items with model-written lines:

```bash
./reelforge.py plan "Top 10 richest people" --items my-items.txt
```

### Exit codes in `run.log`

| Code | Meaning |
|---|---|
| 0 | built |
| 1 | nothing to do (no plan, bad spec, empty result) |
| 2 | dependency missing, or a source is unusable |
| 3 | unexpected — traceback tail is in the log |
| 5 | rate limited (Gemini daily quota, or Pexels hourly). Retry later; cached plans still build offline |

### One build at a time

Wikidata and Commons throttle bursts. Two concurrent builds will 429 and cost
items their entity lookup — which is how "Salar de Uyuni" and the "Great Barrier
Reef" once silently became mood footage. The ladder retries and backs off
adaptively, but serial is the reliable way to run it.

---

## 2. Output

```
output/<slug>/plan.json          the script — edit and rebuild freely
output/<slug>/reel.mp4           the reel
output/<slug>/reel.json          manifest: every shot, its tier, its licence, every take
output/<slug>/reel.caption.txt   caption + hashtags + AI disclosure
output/<slug>/reel.srt           subtitles
output/<slug>/reel.wav           the joined narration
output/<slug>/reel_beats/        one wav + transcript + take stamp per beat
output/<slug>/reel_frames/       per-frame caption/card PNGs (safe to delete)
```

The first thing to read after a build is the tail of the log:

```
every item is on verified footage of the real subject
```

or

```
items NOT on footage of the real subject:
  #3 Tokyo  [GENERIC]
```

---

## 3. The authenticity ladder

Every item resolves to a real Wikidata entity first, which settles its identity
*and* its ambiguity — "Niagara Falls" is a waterfall (Q34221) **and** a city in
Ontario (Q274120), and picking wrong poisons every search after it.

| Tier | How it is proved | Action |
|---|---|---|
| `EXACT` | a stock clip whose page slug names the entity, or a photo Wikidata attaches to it (P18/P10), given motion | none |
| `PROXY` | a real *related* entity (Musk → Tesla, SpaceX) | fine, but check it reads sensibly under that item's line |
| `ILLUSTRATIVE` | a shot **you** named in a spec's `pexels_query` | yours by choice — see below |
| `GENERIC` | nothing matched; mood footage | **investigate** — see §6 |

### ILLUSTRATIVE: when you pick the shot

Some subjects are not photographable. Nobody can identify a county from a
picture, so "authentic footage of Loudoun County" means its courthouse — which
is strictly correct and useless for a reel about wealth.

So a `pexels_query` in a spec is treated as an **instruction, not a fallback**:
it runs before the entity is even resolved, and it wins. The gate does not
disappear, it changes question — from *"is this the subject?"* to *"is this what
was asked for?"*, matching the clip against the query's own words.

That boundary matters. The thing a viewer really would catch is a famous place
shown under another name, so the query's words must appear in the clip's page
slug. Two rounds run at `ILLUSTRATIVE_MATCH_RATIO` (0.34), then one relaxed
round at half that — but the relaxed round only counts **distinctive** words.
Generic scenery words (`_QUERY_SCENERY` in `visuals.py`) can never carry a
relaxed match on their own: `valley` alone matched "Silicon valley tech office"
to a hillside in Turkey, flag visible in frame, captioned SANTA CLARA COUNTY.

The planner can never produce an ILLUSTRATIVE shot — only a hand-written spec.

**Write queries with distinctive words.** "beautiful suburban park maryland"
matched nothing and fell back to a courthouse; "Tysons corner virginia skyline"
and "data centers" both landed.

**Name the activity, not the landscape.** A stock library holds the most
photogenic version of any landscape, and it is almost always somewhere iconic
and somewhere else:

| Query | What came back | Captioned |
|---|---|---|
| `Silicon valley tech office aerial drone` | a hillside in **Turkey**, flag in frame | SANTA CLARA COUNTY, CA |
| `Loudoun County Virginia vineyards` | **Tuscany** — cypresses, olive groves, villa | LOUDOUN COUNTY, VA |
| `data centers` | data centers | LOUDOUN COUNTY, VA ✓ |

Both failures matched on the bare landscape noun, because no clip anywhere
says *Loudoun* or *Santa Clara*. The scenery stoplist catches the first class
(`valley`); the second needs the query written better, so the planner prompt now
says to prefer the distinctive activity — data centers, office parks, container
ports — over landscape nouns like vineyards, mountains or coastline.

Tiers are recorded per shot in `reel.json` under `footage[].tier`.

### YouTube: when the subject is real but too specific for any stock library

Pexels and Commons cover global, generic subjects well; they have nothing for
a named local subject like a specific temple idol or a specific pandal. That
footage exists constantly on YouTube — almost always under YouTube's default
Standard license, which permits none of this. A title reading "FREE TO USE" or
"NO COPYRIGHT" is not a license and was not treated as one: every self-declared
claim like that, checked in September 2026, carried the Standard license when
actually queried, and would have been silent copyright infringement if
trusted. Exactly one real Creative Commons match turned up in that same
search batch — confirming `sources/youtube.py`'s check works, and that
genuine matches for a hyper-specific local subject are rare, not that the
check is broken.

`sources/youtube.py` has exactly one door in: `fetch_by_id`, mirroring
`pinned_shots` — a person watched the actual video, chose the id AND the
in-video timestamp (there is no way to guess a good moment automatically),
and is accountable for both, same as a Pexels pin. It refuses, loudly,
anything that is not verifiably Creative Commons by asking YouTube directly
for that video's own license flag — never by trusting a title.

In a spec, per item:
```json
"youtube_ids": [{"id": "VxiE9Fnv8Wc", "start": 12, "seconds": 6}]
```
`start`/`seconds` default to 0/6 if omitted. Recorded as ILLUSTRATIVE, same as
a Pexels pin — a human's accountable choice, not an automatically-verified
EXACT match. Always carries a credits-card line: CC BY requires attribution,
so `attribution_required` is hardcoded true in the credit dict, unlike
Commons' occasional public-domain case.

**Two ways to find a candidate id, neither of which is itself trustworthy:**
- `sources.youtube.search(query)` — free, no key, scrapes YouTube's own
  search page via `yt-dlp`. A discovery aid ONLY: watch the result yourself
  before ever pinning its id.
- `sources.youtube.search_api(query)` — needs `YOUTUBE_API_KEY` in `.env` (a
  free Google Cloud Console API key — no OAuth, no app review; NOT the same
  credential the publishing side would eventually need). Uses YouTube Data
  API's real `videoLicense=creativeCommon` server-side filter, which is
  reliable in a way scraping titles never can be. Returns `[]` with no key
  set rather than erroring, so callers can try it first unconditionally.

### Why the gate must stay hard

Pexels never returns an empty result set. Asked for "Elon Musk" it reports 1,326
results and offers a drone shot of a beach. Asked for a Bolivian salt flat it
offers `car-driving-across-salt-flats-in-utah`. Ranking is not verification, so a
clip is accepted **only** when the library's own description of it names the
subject. Lowering `SLUG_MATCH_RATIO` below 1.0 is how "Kyoto" silently becomes
"a street in Japan".

### Commons traps, already filtered

A Commons category means *associated with*, not *is*. Left unfiltered it offered
the **Taylor Swift Education Center** as her, a **replica of Niagara Falls in a
shop window** as the falls, Grokipedia **screenshots** and a net-worth **chart**
as Elon Musk, and **AI-generated deepfake videos** of Musk sitting in his own
category. Hence: the Wikidata-pinned file always wins and is exempt from the
heuristics; category files must name the subject, be JPEG, and clear
`config.COMMONS_DENY`. Add to that list rather than loosening it.

---

## 3a. The footage pre-flight

Before a word is voiced, every item is checked against both sources — searches
and Commons metadata only, no downloads, a few seconds for a whole list. The
build prints one line per item:

```
ok   #10 Morris County, New Jersey    commons   4 photo(s), best Morris County Court
NONE #9  Nantucket County, Massachus.           nothing verified (best stock match 0.33)
swapping 1 unfootageable item(s): Nantucket County, Massachusetts
  #9 'Nantucket County, Massachusetts' -> 'Marin County, California' (EXACT via commons)
```

A swapped item is written back to `plan.json`, so a rebuild uses the corrected
list. Replacements are themselves audited before being accepted; one that also
has no footage is rejected and the original is kept.

**When most of the list fails, the topic is the problem, not the items.** If
more than half fail, no swap is attempted — swapping would burn quota
rediscovering the same wall — and the build says so and continues.

Disable with `--no-audit`. Swapping is off automatically for a `spec`, since
your items are the point.

### The pre-flight must not out-call the build

Both ask the same questions, so the answers are cached on disk
(`state/commons_media.json`, `state/entities.json`). Without that, the audit
doubled the API traffic and tripped Commons throttling — the audit would report
an item fine and the build would then find nothing for it. **If you ever see the
audit and the build disagree, a cache is being missed.**

## 4. Narration

Each beat is voiced **separately** and the beat wavs are joined with a 0.15s gap.
This is not a style choice — see §6, "audio repeats itself".

Because each beat's duration is then *measured*, beat spans are exact and
contiguous by construction. No alignment step can misallocate what it is never
asked to allocate, so the rank card, the captions and the footage cannot drift
apart.

Each take is checked before it is accepted:

- duration against word count (~3 words/second; a doubled line lands near 2×)
- a repeated 4-gram from its own script text

A failed check regenerates once. A second failure still ships — the build is
meant to run unattended — but says so in the log and the manifest.

### Take cache

Good takes are kept in `output/<slug>/reel_beats/`, keyed by the line **plus the
voice settings**. Rebuilding after a footage or caption change re-renders the
video without re-voicing a word. Changing `EXAGGERATION`, `CFG_WEIGHT`,
`TEMPERATURE`, `SPEECH_SPEED`, `TTS_MODEL` or `VOICE_SAMPLE` invalidates every
take automatically. To force a re-voice: `rm -rf output/<slug>/reel_beats`.

---

## 5. Format constants

In `config.py`, each with a comment naming the 2026 retention finding behind it.

| Setting | Value | Why |
|---|---|---|
| `TARGET_SECONDS_MIN/MAX` | 42–58s | the 45–60s bracket had the highest engagement rate and ~2× the median views of sub-30s |
| `SHOTS_PER_ITEM` | 2 | a visual change every 1.5–3s holds attention; static past ~4s is the drop-off zone |
| `RANK_CARD_SECONDS` | 1.5 | the reveal is the beat; it then drops to a persistent chip |
| `MIDROLL_TEASE_AT` | 0.55 | plant a fresh open loop every 10–15s or the middle sags |
| `COUNT_DOWN` | true | the unrevealed number one **is** the open loop |
| `CAPTION_SIZE` | 82 | caption guidance for 1080×1920 is 64–88px |
| `CTA_LINE` | asks for a **send** | DM sends are the top-weighted Reels signal in 2026; likes the weakest |

The hook pays off inside ~2s and there is deliberately **no intro card**: filling
the 3–15s window with preamble costs 30–50% of viewers.

The number and name of each item are spoken by the pipeline, not written by the
model, so the numeral burned on screen and the numeral in the voice cannot
disagree. A supplied spec sets `verbatim`, which turns that off because your
`voiceover` already says "Number 10."

### Words-per-second is language-specific, not a universal constant

`config.WORDS_PER_SECOND` drives every pre-flight duration estimate (the
during-build log, and `plan`/`spec`'s printed word count). It is keyed off
`config.ASR_LANGUAGE` via `WORDS_PER_SECOND_BY_LANGUAGE` -- English's 2.8 by
default, Telugu's measured 1.45 when the configured voice is Telugu. Getting
this wrong is exactly how the diabetes-foods spec's first draft reported
"58s, in the target band" for a script that actually ran 109s: Telugu says
roughly half as much per word as English does, and the estimate did not
know that. Add an entry to that dict before trying a new language, or the
same silent mismatch happens again. `narrate.py` keeps its own,
more-precisely-calibrated version of this (`_WORDS_PER_SECOND_BY_LANGUAGE`)
for the per-take quality check specifically, deliberately separate from
this one; see its comment for why.

---

## 6. Failure modes

### Audio repeats itself, truncates, or mangles a name

**The symptom looks like a rendering bug and is not.** Chatterbox degrades on
long input. A 134-word single utterance produced:

```
Number 5, Sydney. Opera House Roof uses over 1 million tiles.
Number 4, Istanbul is stable.                      <- truncated to nonsense
And number 5, Sydney. Opera House Roof uses ...    <- ENTIRE ITEM SAID TWICE
Number 1, Vanessa.                                 <- "Venice"
```

Diagnose by transcribing what shipped and comparing word counts — never by
inspecting frames:

```bash
cd output/<slug>
../../.venv/bin/python -c "
import json,collections
w=[str(x['word']).strip() for x in json.load(open('reel.words.json'))]
print(len(w),'words heard'); print(' '.join(w))
n=[x.lower().strip('.,!?') for x in w]
g=collections.Counter(tuple(n[i:i+4]) for i in range(len(n)-3))
print('REPEATED:',[k for k,c in g.items() if c>1] or 'NONE')"
```

Heard-word count should equal the script's. If it does not, a take slipped
through: `rm -rf output/<slug>/reel_beats` and rebuild. Keep any single
Chatterbox call to one sentence or beat.

### Non-Latin captions render as blank boxes

`videocomposite._font()` and `thumbnail._compose()` used to always load
`config.DISPLAY_FONT` (Anton-Regular.ttf) -- a Latin-only display font.
Installing libraqm (see the Telugu localisation commit) fixes glyph
SHAPING, i.e. which glyphs to combine for a conjunct or vowel sign; it does
nothing when the font has no glyph for the codepoint in the first place,
which Anton never did for Telugu. PIL draws a silent `.notdef` blank box
per missing glyph rather than erroring, so this shipped in three earlier
Telugu builds before anyone caught it.

Fixed by `_font(size, text="")`: it picks `fonts/NotoSansTelugu-Bold.ttf`
whenever `text` contains a codepoint in the Telugu Unicode block
(`ఀ`-`౿`, U+0C00-U+0C7F), else falls back to `DISPLAY_FONT`. Every call
site that draws a spec's actual localised text (caption words, an item's
name plate) must pass that text in; call sites drawing brand furniture
("NO.", "4/5", the follow handle) are correctly untouched, since that
text is meant to stay in the brand's display font regardless of language.
Adding a new script (Hindi, etc.) means adding both its font file and a
`_is_<script>()` range check the same way.

### Non-English captions go blank for most of a sentence, then catch up

Whisper had no language hint anywhere in the pipeline, so it auto-detected
the spoken language from each beat's audio -- and on a short (7-9s)
non-English clip it guesses wrong often enough to matter. A Telugu beat's
actual transcription: `"lho munchi kovulu, protein lho peechu untai. [two
Kannada chars] [Gujarati text] lho chakker s'thāyilu..."` -- phonetic
nonsense mixing three scripts, sharing not one real word with the actual
script apart from a bare rank digit. `retime_script`'s alignment (see § 4)
had exactly that one anchor to work with; every word after it fell into the
"no anchor" interpolation branch, which compresses forward from the last
known anchor at ~0.14s per word rather than spreading across the beat's
real duration -- so the whole sentence's captions collapsed into the first
~2 seconds, then sat frozen (or blank) for the rest of the beat while the
voice kept talking. This was present in every Telugu build from the first
one; short beats or a viewer not scrutinising captions closely enough are
the only reason it went uncaught for three builds.

Fixed by `config.ASR_LANGUAGE`, derived from `EDGE_TTS_VOICE`'s own prefix
("te-IN-ShrutiNeural" -> "te") so a voice switch cannot leave it stale, and
threaded through `voiceclone.transcribe_words` -> `reel_worker.py asr
--language` -> Whisper's own `language=` parameter. Diagnose by
transcribing a beat directly and checking whether it comes back in the
right SCRIPT, not just checking word count:
```bash
.venv-reel/bin/python reel_worker.py asr --audio output/<slug>/reel_beats/bNN.wav \
  --out /tmp/check.json --model mlx-community/whisper-large-v3-turbo --backend mlx_whisper
python3 -c "import json; print(json.load(open('/tmp/check.json'))['text'])"
```
If that comes back in the wrong script or as phonetic Latin nonsense, the
language hint isn't reaching Whisper (check `ASR_LANGUAGE` derived correctly
from the voice in use, or set it explicitly). Cached beat audio and
`.words.json` survive a rebuild via `_take_key` (keyed on TTS settings only,
not ASR settings) -- after fixing the language hint, `rm -rf
output/<slug>/reel_beats` before rebuilding, or the stale bad transcription
gets reused unchanged.

### A sentence's captions rush at the very end, even with the language hint fixed

The language hint (above) gets Whisper transcribing CLOSE to the real
script, but not exactly -- "నెమ్మదిగా" comes back as "నమ్మదిగా",
"రక్తంలో" as "రక్తల్లో". `retime_script`'s matching is EXACT string
equality (`difflib.SequenceMatcher`), so a near-miss spelling counts as no
match at all, same as if Whisper had heard nothing. One real beat matched
only "ఇది" ("this") as its last anchor, 4.5s into a 7.58s clip; the five
words after it (over half the sentence) used to get a fixed ~0.14s slot
each, counting forward with no notion of how much real audio was left --
the whole tail flashed by in under a second while the voice kept speaking
for three more seconds with no caption on screen.

Fixed by passing `duration` (the beat's own `probe_duration`-measured wav
length) into `retime_script`: unmatched leading/trailing words now spread
across `[0, first anchor]` / `[last anchor, duration]` rather than a fixed
step count, so the interpolation is always bounded by the clip's REAL
length regardless of how few words happened to match exactly. Diagnose by
comparing a beat's last caption word's `end` against its wav's actual
duration (`ffprobe -show_entries format=duration`) -- they should be equal;
a caption ending noticeably before the audio does is this bug.

### Telugu script over-translates specific terms

A generated Telugu script must TRANSLITERATE (keep the actual English
word, in Telugu script) a specific named thing or a technical term, and
only TRANSLATE genuinely descriptive language. This already applied to
place names (see the intro's `pexels_query` note in spec.py); the same
rule turned out to apply to food/ingredient category names and medical
terms once tested on a real topic: Gemini's first pass rendered "diabetes"
as "మధుమేహం" and "Nuts and Seeds" as "గింజలు మరియు విత్తనాలు" -- both
technically valid dictionary Telugu, but not what this audience actually
calls either thing, which changes the meaning they take from it. Genuine
native names for a specific food (కాకరకాయ for bitter gourd, మెంతులు for
fenugreek, రాగి for ragi) are correct as-is and must not be touched --
the rule is about not inventing or forcing a translation, not about
banning Telugu script itself. When this shows up, send the spec back to
Gemini with the specific words to transliterate named explicitly; a vague
instruction to "keep names in English" was not enough to stop it forcing
a category translation.

### The audit says ok, the build says GENERIC

They ask the same sources, so they can only disagree when a cache is missed or a
download fails. Check the log for `commons download failed: 429`. Downloads go
through the same rate limiter as API calls and fetch a 1600px thumbnail rather
than the original — Wikimedia's own 429 message asks for exactly that, and a
12288px panorama was once being fetched to render a 1080px video. If this
recurs, raise `commons._MIN_INTERVAL`.

### An item falls to GENERIC

In order of likelihood:

1. **Throttling.** Check the log for `wikidata: ... 429`. Run one build at a
   time; the ladder retries but sustained load beats it.
2. **A comma-qualified name.** "Salar de Uyuni, Bolivia" is titled "Salar de
   Uyuni" in Wikidata. The resolver falls back to the part before the comma —
   if a new pattern appears, that is where to extend it.
3. **The library genuinely has nothing**, which for a Bolivian salt flat is the
   honest answer: Pexels only had Utah. Commons then carries it as a still.

Diagnose one name in isolation:

```bash
.venv/bin/python -c "
import sys; sys.path.insert(0,'.')
import entities; from sources import stock, commons
e=entities.resolve('Tokyo','place')
print(e.qid, repr(e.label), 'err=',repr(e.error))
print('identifiers:',[sorted(s) for s in e.identifiers()])
print('commons:', len(commons.entity_media(e)))
for v in stock.search('Tokyo')[:6]:
    print(max(stock.match_ratio(v,s) for s in e.identifiers()), stock._slug(v))"
```

### The wrong entity resolved

Check `e.label`. "Tokyo" once resolved to `Q308891` *"ward area of Tokyo"*, which
made the gate demand *ward* and *area* in every slug and rejected genuine Tokyo
footage. An exact label match now outranks Wikidata's own ordering, and
verification keys off the name **as written** so a bad resolution can never
*tighten* the gate. If a name still resolves wrong, set its `kind` in
`plan.json` and rebuild.

### Cache poisoning after a logic change

`state/entities.json` is keyed with `_CACHE_VERSION` (currently 3). **Bump it
whenever resolution logic changes** — the "ward area of Tokyo" bug survived its
own fix until that existed. Nuclear option: `rm state/entities.json`.

### Gemini says quota exhausted

Free tier is 20 requests/day on `gemini-2.5-flash`. Every response is cached
under a hash in `state/plans/`, so builds and rebuilds are free; only `--refresh`
spends quota. A cached plan still builds with no network to Google at all.

### Pexels 429

200 searches/hour. One search per phrasing per run, cached in-process. Wait.

### A slug match hides content nobody would want on screen

The slug/word gate proves a clip matches the QUERY's words; it says nothing
about whether the clip is otherwise appropriate. `howto-flat-tire.json` rank 1
("Pull over safely", query `driver stopped car road`) matched a stock clip
titled, literally, *"a boy pretending to drive a car"* — thematically on-topic
by the gate's own rules, and wrong content for a driving-safety tutorial. Caught
2026-09-24 by watching the actual composited frame, not by any automated check
— nothing in the pipeline screens for this. Fixed by pinning known-good ids
(`pexels_ids: [27374367, 29142341]`), the same mechanism as a bad
"jack vs. lift" ambiguity. **Watch the actual frames of anything with people in
it before trusting the tier label.**

### Follow card collided with the caption band and doubled the handle

`videocomposite.py`'s closing follow card sits at `REEL_H - SAFE_BOTTOM - 150`
(≈1410px); the caption band sits at `REEL_H * CAPTION_BAND` (≈1382px) — 28px
apart, so the CTA line's last words rendered directly through the "SEND THIS"
card. The persistent quiet handle was also never suppressed once the follow
card drew its own handle underneath the card, so `@handle` appeared twice
stacked. Fixed 2026-09-24: the quiet handle now only draws while `card < 0`,
and the caption is lifted to `REEL_H * 0.60` for the frames the follow card is
on screen. **This bug is specific to howto/countdown reels with both karaoke
captions and the follow card active in the closing seconds — check the other
project's `videocomposite.py` if it ever grows a follow card too**, since the
two files are forked copies and this fix was applied only here.

---

## 7. Known gaps

| Gap | Detail |
|---|---|
| **No retention sweep** | `state/assets/` reached ~1GB across a few topics and nothing prunes it. Safe to delete entirely; everything re-downloads. |
| **No publishing** | Deliberate. There is no `--post`, no scheduler, no LaunchAgent. |
| **Some topics are visually hostile** | The ladder guarantees the footage is *of the subject*, not that the subject is interesting to look at. "Top 10 richest counties" scored 10/10 EXACT and is mostly courthouses, because a county's only widely photographed object is its courthouse. Prefer topics whose items are things people actually photograph. `topics` reports this as `footage_risk`. |
| **Spec durations are advisory** | `duration_sec` is a floor; speech cannot be truncated without clipping a word, so a beat runs as long as it takes. |
| **CC BY / BY-SA credits** | Rendered on a closing card. If you set `CREDITS_CARD=false`, the attribution must go in the post caption — it is a licence obligation, not a design choice. |
| **Forked, not shared** | `videocomposite.py`, `voiceclone.py`, `reel_worker.py`, `screening.py` are copies of the AI_News files. Fixes must be carried across by hand. |

---

## 8. Reset and rollback

```bash
# re-voice one reel from scratch
rm -rf output/<slug>/reel_beats

# rebuild everything for one topic, keeping the script
rm -rf output/<slug>/reel_frames output/<slug>/reel.mp4

# forget every entity lookup (after changing resolution logic)
rm state/entities.json

# forget every downloaded clip and photo (~1GB)
rm -rf state/assets

# forget every Gemini plan — THIS COSTS QUOTA to regenerate
rm -rf state/plans
```

Nothing is scheduled, so there is nothing to stop.

---

## 9. Files

| File | Role |
|---|---|
| `reelforge.py` | CLI: topics / plan / build / make / spec / probe |
| `planner.py` | Gemini → countdown script, written to the format spec, cached |
| `spec.py` | your hand-written JSON → the same plan structure |
| `entities.py` | name → Wikidata entity, its identifying words, its proxies |
| `sources/stock.py` | Pexels, behind the verification gate |
| `sources/commons.py` | Wikimedia Commons, filtered hard |
| `visuals.py` | the ladder: item → verified shots + tier |
| `kenburns.py` | still → moving 9:16 shot |
| `narrate.py` | per-beat voicing, take validation, take cache |
| `build.py` | beats → voice → shots → mp4 → manifest |
| `videocomposite.py` | rank cards, karaoke captions, ffmpeg assembly *(forked)* |
| `voiceclone.py`, `reel_worker.py` | Chatterbox TTS + Whisper word timings *(forked)* |
| `screening.py` | optional face rejection *(forked)* |

`REEL_VENV` in `.env` points at the AI_News `.venv-reel` so the ~3GB torch/mlx
stack is not installed twice. **Deleting or rebuilding that venv breaks
ReelForge's voice step.**

## Thumbnails (`thumbnail.py`) *(2026-09-20)*

The tile is the whole click decision on a channel grid and in search, and there
was none -- YouTube grabbed whatever frame it liked, which for a countdown is
usually a rank card mid-animation or a caption caught between pages.

```bash
./thumbnail.py <slug>              # thumbnail.jpg (1080x1920) + thumbnail_16x9.jpg (1280x720)
./thumbnail.py <slug> --rank 4     # force a particular item's footage
```

`build.py` calls it at the end of every build, and a failure there is logged
rather than raised: the reel is already rendered and written by that point, so
a missing thumbnail is something to regenerate, never a reason to lose a render.

**The background is the reel's OWN sourced footage.** This project exists to
prove the footage under an item really is that item, so pulling a thumbnail
from a fresh stock search would advertise the one thing the pipeline refuses to
do. Frames come from the cached clip, not the finished mp4 -- the render
already has rank cards and karaoke captions burned in, and a thumbnail built on
those is text over text.

**It does not spoil number one.** Ranks 2-5 first, then any other real item,
never rank 1. Nothing on the tile names the item, so the picture alone gives no
rank away.

**Tier ladder, not EXACT-only.** EXACT first, then ILLUSTRATIVE, then the
hook's generic mood footage as a last resort. An EXACT-only rule looked right
until "Top 10 richest counties" was tried: not one of its ten items has exact
footage, so all ten were skipped and the generic cloud shot went on the tile.
A whole class of topic would have shipped that way.

Caveat worth knowing: on an ILLUSTRATIVE topic the tile shows footage that is
not verifiably the item. "Richest US counties" currently picks a frame that
reads as an English town. That is the same tier the reel itself uses for those
items, so the thumbnail is no less accurate than the video -- but it is the
most visible surface, so it is worth an eye before uploading.

**The "TOP 10" badge is derived when the title lacks it.** "Richest US
Counties" is a real plan title with no count in it, and the badge is the genre
signal that makes someone read the rest of the tile, so the item count fills in.

Wide type is sized larger relative to its canvas than vertical type: a search
result is a couple of hundred pixels across, and type scaled to look right at
full size vanishes there.

## Which account this posts to *(2026-09-20)*

ReelForge is its own repo (`pvn1987-code/skillificationed`, private) and its
own channel. It shares NOTHING with pa1kura.ai except three per-service keys
that are quotas rather than identity: Gemini, Pexels and the Vercel blob token.

**The trap.** This project was forked from AI_News and the working `.env` came
with it, so `IG_USER_ID`, `THREADS_USER_ID` and `BRAND_HANDLE` were still
@pa1kura's on 2026-09-20. No harm done yet, because there is no publisher in
this repo -- but the first one added will post countdown reels to the AI-news
account unless those three are repointed first. Repoint them BEFORE writing
any publish code, not after.

The two pipelines also share the TTS venv (`.venv-reel`) and nothing else.
Changes to the renderer, captions or voice clone do not propagate between them;
they were copied at fork time and have diverged since.

## Footage audit of the 30-day calendar *(2026-09-20)*

30 representative items, two from each countdown topic, run through
`visuals.audit_item`. Answering "will this actually be real footage?" before
committing a month of production to it.

**Everything resolves. Almost none of it is video.**

| outcome | items | what the viewer sees |
|---|---|---|
| EXACT via stock | 7 | real **video** of the subject |
| PROXY via stock | 1 | real video of a stand-in (Finland → Helsinki) |
| EXACT via commons | 20 | real **photographs**, Ken Burns pan |
| GENERIC | 2 | unrelated stock |

So 28 of 30 get authentic material, but only **8 of 30 move**. Two thirds are
stills. Famous places and brands have video (Golden Gate 60 clips, Dubai 53,
Tokyo 36); named objects -- a yacht, a gemstone, a dragline excavator -- exist
only as Commons photographs. Expect Yachts, Gemstones and Heaviest Objects to
be near-slideshows, and plan the Ken Burns settings accordingly.

Genuine misses, both named objects: "Eclipse (yacht)" (best stock match 0.50)
and "Gullfaks C" (0.00).

### Two false EXACTs, which is the real problem

* **Azzam** → `Caroline Azzam.jpg`. A person, not the superyacht.
* **Methuselah** → `Methuselah Stained glass.jpg`. The biblical figure in a
  church window, not the 4,800-year-old bristlecone pine.
* **Fort Knox** → `An UH-1 Iroquois helicopter...`. Defensible (it is the base)
  but a poor visual for "most guarded vault".

All three reported **EXACT**, the tier that means verified. A single-word or
shared proper noun resolves to whatever Wikidata ranks first, and nothing
checks that the entity's TYPE matches the kind asked for. For a pipeline whose
entire claim is that the footage is really the subject, a confident wrong
answer is worse than no answer: `audit_item("Azzam", "thing")` should refuse a
human, and `("Methuselah", "thing")` should refuse a stained-glass window.

### Wikidata unreachability silently downgrades to GENERIC

Six of the thirty first came back "wikidata unreachable after 4 tries". Re-run
one at a time with a pause, every one resolved -- the audit was rate-limiting
itself. Worth knowing for two reasons: audit in small batches, and more
importantly a Wikidata outage during a real build does not fail the build, it
quietly drops those items to GENERIC footage. The authenticity report records
it, so check that before publishing rather than assuming a clean run.
