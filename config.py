"""Shared configuration for ReelForge. Everything tunable lives here or in .env.

The numbers in the FORMAT section are not taste -- each one is set from
published 2026 retention data for short-form video, and the comment on each
says which finding it came from. Change them knowing what you are trading.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

# --- Secrets -----------------------------------------------------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY", "").strip()
# Optional. Unlocks sources.youtube.search_api()'s real videoLicense=
# creativeCommon filter -- a free API key (Google Cloud Console, no OAuth, no
# app review), not the OAuth/upload credential the publishing side would need.
# Search still works without it (sources.youtube.search()), just less reliably.
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "").strip()
# Wikimedia asks for a descriptive UA with contact details on API traffic.
WIKI_USER_AGENT = os.getenv(
    "WIKI_USER_AGENT",
    "ReelForge/0.1 (personal countdown-reel project; contact via GitHub)").strip()

# --- Paths -------------------------------------------------------------------
OUTPUT_DIR = ROOT / "output"
STATE_DIR = ROOT / "state"
ASSET_CACHE = STATE_DIR / "assets"
# Every Gemini response is cached by topic hash. Re-running a build, or
# rebuilding after a render bug, must never spend a second free-tier call.
PLAN_CACHE = STATE_DIR / "plans"
LOG_FILE = ROOT / "run.log"
# The TTS/ASR venv carries torch and mlx -- about 3GB. An absolute path is
# honoured so this project can point at an existing install (the news
# pipeline's .venv-reel) instead of downloading the whole stack twice.
_venv = os.getenv("REEL_VENV", ".venv-reel")
REEL_VENV = Path(_venv) if os.path.isabs(_venv) else ROOT / _venv

# --- Brand -------------------------------------------------------------------
# NO DEFAULT, deliberately. This is burned into every frame of every reel, and
# it defaulted to "@pa1kura" -- the AI-news channel this project was forked
# from -- so the first flat-tire build rendered 29 seconds of video watermarked
# with the wrong account. A config default is not a safe place to keep another
# channel's identity: an unset variable should produce no handle, never
# somebody else's. Set BRAND_HANDLE in .env.
BRAND_HANDLE = os.getenv("BRAND_HANDLE", "").strip()
DISPLAY_FONT = os.getenv("DISPLAY_FONT", "Anton-Regular.ttf")
# Disclosure. Stated in the caption; never burned into the frame.
#
# The publisher already sends is_ai_generated=true, which is what satisfies the
# platform's disclosure requirement and makes it render its own label. A second
# label burned into the video is redundant, and it sat at the top of frame
# through the first two seconds -- the exact window the hook has to land in.
# Worth remembering that only the VOICE is synthetic here: every clip and
# photograph is real, licensed footage of the real subject.
DISCLOSURE_LINE = os.getenv(
    "DISCLOSURE_LINE",
    "Narration is an AI voice. All footage is real, licensed stock and "
    "Wikimedia Commons material of the places shown.").strip()

# --- Canvas ------------------------------------------------------------------
REEL_W, REEL_H = 1080, 1920
REEL_FPS = int(os.getenv("REEL_FPS", "30"))

# === FORMAT: the countdown itself ===========================================
# Socialinsider's 2026 read of ~140k Reels put the 45-60s bracket at the highest
# engagement rate and roughly double the median views of sub-30s. Ten items at
# ~4s plus hook and close lands inside that window; the planner is told to
# write to it and the build warns when the rendered result falls outside.
TARGET_SECONDS_MIN = float(os.getenv("TARGET_SECONDS_MIN", "42"))
TARGET_SECONDS_MAX = float(os.getenv("TARGET_SECONDS_MAX", "58"))

# Ten is the format's own promise ("top 10"), so it is the default. Seven is
# the honest fallback when a subject cannot fill ten -- padding a list with a
# weak entry costs more retention than a shorter list does.
ITEM_COUNT = int(os.getenv("ITEM_COUNT", "10"))

# Count DOWN. The unrevealed number one is the open loop that carries the whole
# video; counting up spends the payoff first and the rest is downhill.
COUNT_DOWN = os.getenv("COUNT_DOWN", "true").strip().lower() == "true"

# Retention work is consistent that a visual change every 1.5-3s holds
# attention and that a static shot past ~4s is where viewers leave. An item
# gets about 4s, so it is deliberately cut into TWO shots rather than held.
SHOTS_PER_ITEM = int(os.getenv("SHOTS_PER_ITEM", "2"))
REEL_MIN_SHOT = float(os.getenv("REEL_MIN_SHOT", "1.5"))
REEL_MAX_SHOT = float(os.getenv("REEL_MAX_SHOT", "3.0"))

# The number card is the beat. It animates in on the cut and holds while the
# item is named, then drops back to a small persistent rank chip -- the chip is
# what tells a viewer who joined mid-scroll that the list is not finished yet.
RANK_CARD_SECONDS = float(os.getenv("RANK_CARD_SECONDS", "1.5"))

# The word above the numeral on the reveal card. A countdown counts DOWN to a
# reveal, so "NO. 3" is right; a tutorial counts UP through a sequence, where
# "NO. 3" reads as a ranking that does not exist and actively misleads -- step
# three is not the third-best step. Selected by the plan's `format`.
RANK_LABEL = os.getenv("RANK_LABEL", "NO.")
STEP_LABEL = os.getenv("STEP_LABEL", "STEP")
RANK_CHIP = os.getenv("RANK_CHIP", "true").strip().lower() == "true"

# A fresh open loop every 10-15s is what stops the middle of a list sagging.
# The planner writes one tease line and it is placed at this fraction through.
MIDROLL_TEASE_AT = float(os.getenv("MIDROLL_TEASE_AT", "0.55"))

# DM sends are the top-weighted Reels signal in 2026 and likes the weakest, so
# the close asks for a send, not a follow. Fixed wording: a call to action that
# is reworded every build is just noise.
CTA_LINE = os.getenv(
    "CTA_LINE",
    "Send this to the one person you'd take to number one.").strip()
CTA_CARD_TEXT = os.getenv("CTA_CARD_TEXT", "SEND THIS").strip()
CTA_SECONDS = float(os.getenv("CTA_SECONDS", "2.6"))

# --- Captions ----------------------------------------------------------------
# Karaoke: whole phrase on screen, spoken word lit. It is the caption style
# that dominates top-performing short-form, and it gives a muted autoplay
# viewer something to track instead of reading ahead.
KARAOKE = os.getenv("KARAOKE", "true").strip().lower() == "true"
CAPTION_WORDS = int(os.getenv("CAPTION_WORDS", "3"))
# Caption guidance for 1080x1920 puts the body between 64 and 88px. The old
# news reel ran 104, which is louder than it needs to be and leaves no room for
# the rank card above it.
CAPTION_SIZE = int(os.getenv("CAPTION_SIZE", "82"))

# Where the caption band sits, as a fraction of frame height. 0.72 puts it in
# the lower third, clear of the subject and above the platform's own furniture.
# It used to be computed as (REEL_H - SAFE_BOTTOM)/2, which is 0.41 -- the
# middle of the frame, straight over whatever the shot was showing.
CAPTION_BAND = float(os.getenv("CAPTION_BAND", "0.72"))
HIGHLIGHT = os.getenv("HIGHLIGHT", "#FFC43D").strip()
CAPTION_IDLE = os.getenv("CAPTION_IDLE", "#FFFFFF").strip()
HIGHLIGHT_SCALE = float(os.getenv("HIGHLIGHT_SCALE", "1.08"))
# Instagram lays its own caption, handle and buttons over this strip.
SAFE_BOTTOM = int(os.getenv("SAFE_BOTTOM", "360"))

# --- Voice -------------------------------------------------------------------
# Two engines. Chatterbox (local, mlx_whisper worker) can clone a voice from a
# reference clip, but VOICE_SAMPLE has never actually been set here -- so it
# was paying Chatterbox's costs (occasional rough takes, the long-input
# degradation narrate.py's docstring documents) for a cloning capability
# nothing was using. Edge TTS (Microsoft's free cloud neural voices, via the
# `edge-tts` package) is the default instead: mature, consistently clean, and
# needs none of the heavy REEL_VENV torch/mlx stack for synthesis itself --
# though that venv is still required for Whisper caption timing either way.
# Chosen 2026-09-24 after listening to three candidates side by side.
TTS_ENGINE = os.getenv("TTS_ENGINE", "edge").strip().lower()
EDGE_TTS_VOICE = os.getenv("EDGE_TTS_VOICE", "en-US-ChristopherNeural").strip()
# Natural pace, no rate boost -- "I don't want a rushed-speaking video... it
# should not be pushy." If a script runs long, the fix is fewer beats, never a
# faster read. Leave at "+0%" unless explicitly asked to change the pace.
EDGE_TTS_RATE = os.getenv("EDGE_TTS_RATE", "+0%").strip()

TTS_MODEL = os.getenv("TTS_MODEL", "mlx-community/chatterbox-fp16").strip()
VOICE_SAMPLE = os.getenv("VOICE_SAMPLE", "").strip()
EXAGGERATION = float(os.getenv("EXAGGERATION", "0.7"))
CFG_WEIGHT = float(os.getenv("CFG_WEIGHT", "0.3"))
TEMPERATURE = float(os.getenv("TEMPERATURE", "0.8"))
# Chatterbox reads at ~190wpm and ignores its own speed argument, so this is an
# ffmpeg atempo stretch on the rendered wav. A countdown wants more attack than
# the news read, so it sits slightly faster. Below ~0.85 atempo smears
# consonants; above ~1.05 the item names stop landing. Applies to either
# engine's output, but leave it at 1.0 for Edge -- see EDGE_TTS_RATE above.
SPEECH_SPEED = float(os.getenv("SPEECH_SPEED", "1.0"))
ASR_BACKEND = os.getenv("ASR_BACKEND", "mlx_whisper").strip()
ASR_MODEL = os.getenv("ASR_MODEL", "mlx-community/whisper-large-v3-turbo").strip()
# Without this, Whisper auto-detects the spoken language from the audio --
# and on a short (7-9s) non-English clip it guesses wrong often enough to
# matter: a Telugu beat came back transcribed as Kannada/Gujarati-flavoured
# phonetic nonsense sharing not one real word with the actual script, so
# caption timing (which aligns Whisper's words against the script) had
# almost nothing to anchor on. Derived from EDGE_TTS_VOICE's own language
# prefix ("te-IN-ShrutiNeural" -> "te") so a voice switch cannot leave this
# stale; ASR_LANGUAGE overrides it explicitly when set, and "" restores
# auto-detect (e.g. for Chatterbox, whose cloned voice has no such prefix).
ASR_LANGUAGE = os.getenv("ASR_LANGUAGE", "").strip() or (
    EDGE_TTS_VOICE.split("-", 1)[0].lower()
    if TTS_ENGINE == "edge" and len(EDGE_TTS_VOICE.split("-", 1)[0]) == 2 else "")

# The pre-flight word-count estimate (build.py, reelforge.py's `plan`/`spec`
# commands) and narrate.py's per-take quality check both used to hardcode an
# English rate (2.8, 2.2) regardless of language. Harmless for English, but
# for Telugu the real measured rate is roughly half that -- a script whose
# word count "estimated 58s, in the target band" actually ran 109s once
# spoken, because the estimate never knew it was about to be read in Telugu.
# One table, keyed the same way ASR_LANGUAGE is, so every caller agrees.
# Extend this as more languages are added; an unlisted language keeps the
# English rate rather than guessing.
WORDS_PER_SECOND_BY_LANGUAGE = {"te": 1.45}
WORDS_PER_SECOND = WORDS_PER_SECOND_BY_LANGUAGE.get(ASR_LANGUAGE, 2.8)

# --- Visual sourcing: the authenticity ladder -------------------------------
# The whole point of this project. Pexels never returns an empty result set --
# "Elon Musk" returns a drone shot of a beach -- so a keyword search alone will
# happily present anything as anything. Footage is therefore only accepted when
# something OUTSIDE the search ranking confirms the subject.
#
#   EXACT    the asset's own metadata names the entity (a Pexels page slug
#            reading "niagara-falls", or a file Wikidata itself attaches to
#            the entity). This is the only tier allowed to carry an item.
#   PROXY    a real, verified thing strongly associated with the entity, when
#            the entity itself has no usable footage: Musk -> a genuine Tesla
#            factory line. Still authentic, just not the subject.
#   GENERIC  matched nothing; mood footage only. Never carries an item name.
#   ILLUSTRATIVE  a shot a PERSON asked for by name in a spec's pexels_query.
#            The gate exists to stop the MACHINE silently substituting one
#            thing for another. A human writing "luxury suburban neighborhood
#            aerial" under Forsyth County is making an editorial choice about
#            how to convey wealth, and is accountable for it. It is recorded as
#            ILLUSTRATIVE, never as the subject, and the planner can never
#            produce one -- only a hand-written spec can.
TIER_EXACT, TIER_PROXY, TIER_GENERIC = "EXACT", "PROXY", "GENERIC"
TIER_ILLUSTRATIVE = "ILLUSTRATIVE"

# When YOU supply a pexels_query, the gate stops asking "is this the subject?"
# and starts asking "is this what was asked for?" -- because for something like
# a county, nobody could identify it from a photograph anyway, so a shot of a
# luxury suburb is the honest way to convey it and a courthouse is not.
#
# The query then becomes its own gate. This is the share of the query's own
# words the clip's page slug must carry. It is what stops "luxury suburban
# neighborhood aerial" returning the Statue of Liberty -- a famous place shown
# under someone else's name is the one substitution a viewer WOULD catch.
# Share of the author's own words a clip's slug must carry. Measured against
# the first flat-tire build (2026-09-20), where every pick was scored: the
# clips that actually showed the step scored 0.50-0.75, and every clip that
# did not scored 0.33 or less -- "pull over safely" landed on a couple
# drinking coffee at 0.25, "fit the spare" on a man inspecting a tyre at 0.33.
# The split was clean, so the threshold sits between the two groups.
ILLUSTRATIVE_MATCH_RATIO = float(os.getenv("ILLUSTRATIVE_MATCH_RATIO", "0.5"))

# The last-chance round, for a descriptive phrase that shares few words with
# any slug. It used to be half the main ratio, which at the old 0.34 meant
# 0.17 -- one common word carried a match, and that is exactly how the coffee
# couple got in. An explicit floor instead, so raising the main ratio cannot
# silently loosen the fallback.
ILLUSTRATIVE_RELAXED_RATIO = float(os.getenv("ILLUSTRATIVE_RELAXED_RATIO", "0.34"))

# How many of the entity's distinctive words a stock page slug must contain
# before the clip counts as EXACT. 1.0 = all of them. Dropping this is how
# "Kyoto" silently becomes "a street in Japan".
SLUG_MATCH_RATIO = float(os.getenv("SLUG_MATCH_RATIO", "1.0"))
MIN_CLIP_SECONDS = int(os.getenv("MIN_CLIP_SECONDS", "5"))
MIN_CLIP_WIDTH = int(os.getenv("MIN_CLIP_WIDTH", "1080"))

# Commons categories carry plenty that is not usable footage of the subject:
# signatures, net-worth charts, PDFs, and -- the dangerous one -- AI-generated
# deepfakes. Anything whose title matches these never enters a reel.
COMMONS_DENY = [t.strip().lower() for t in os.getenv(
    "COMMONS_DENY",
    # Not usable footage of anything.
    "signature|logo|coat of arms|map|graph|chart|diagram|net worth|timeline|"
    "source code|screenshot|poster|book cover|flag of|stamp|banner|"
    # Fabricated or editorialised likenesses. The Elon Musk category really
    # does return AI deepfake videos, which would be ruinous to broadcast.
    "ai-generated|ai generated|deepfake|parody|caricature|cartoon|meme|"
    # Things NAMED AFTER or DEPICTING the subject rather than being it. The
    # first pass offered the "Taylor Swift Education Center" as Taylor Swift
    # and a "Replica of Niagara Falls" in a shop window as Niagara Falls.
    "replica|model of|miniature|education center|education centre|museum|"
    "memorial|statue|waxwork|madame tussauds|mural|street art|graffiti|"
    "collection|collage|merchandise|plaque|grave|tombstone|endorsed|tweet|"
    "exhibition|lego|painting of|portrait of|drawing of|sketch"
).split("|") if t.strip()]
COMMONS_MIN_WIDTH = int(os.getenv("COMMONS_MIN_WIDTH", "1000"))
# Per entity. More than this and one item's look starts to dominate the reel.
COMMONS_MAX_FILES = int(os.getenv("COMMONS_MAX_FILES", "4"))

# CC BY and CC BY-SA files legally require credit, so every non-public-domain
# asset used is collected and rendered on a closing credits card.
CREDITS_CARD = os.getenv("CREDITS_CARD", "true").strip().lower() == "true"
CREDITS_SECONDS = float(os.getenv("CREDITS_SECONDS", "2.0"))

# Ken Burns: a still has to move or it reads as a dead frame in a feed. Slow
# push plus a little drift, never enough to reveal the crop edge.
KENBURNS_ZOOM = float(os.getenv("KENBURNS_ZOOM", "1.12"))
KENBURNS_SECONDS = float(os.getenv("KENBURNS_SECONDS", "5.0"))

# Mood footage for beats with no subject of their own (hook, tease, close).
# Never used to stand in for a named item.
GENERIC_VISUALS = [v.strip() for v in os.getenv(
    "GENERIC_VISUALS",
    "aerial clouds sunrise|city street at night|ocean waves slow motion|"
    "abstract light streaks|mountain landscape drone"
).split("|") if v.strip()]

# --- Face screening (off by default, as in the news pipeline) ----------------
REJECT_FACES = os.getenv("REJECT_FACES", "false").strip().lower() == "true"
REEL_FACE_MODEL = STATE_DIR / "models" / "yunet.onnx"
REEL_FACE_SAMPLES = int(os.getenv("REEL_FACE_SAMPLES", "6"))
REEL_FACE_MIN_HITS = int(os.getenv("REEL_FACE_MIN_HITS", "2"))

# --- Compatibility shims ----------------------------------------------------
# videocomposite.py, voiceclone.py, screening.py and reel_worker.py are forked
# from the news pipeline and still read the REEL_* names. Aliasing here keeps
# those files close to their originals so fixes can be carried across.
REEL_LOG = LOG_FILE
REEL_BROLL_CACHE = ASSET_CACHE
REEL_CAPTION_WORDS = CAPTION_WORDS
REEL_CAPTION_SIZE = CAPTION_SIZE
REEL_KARAOKE = KARAOKE
REEL_HIGHLIGHT = HIGHLIGHT
REEL_CAPTION_IDLE = CAPTION_IDLE
REEL_HIGHLIGHT_SCALE = HIGHLIGHT_SCALE
REEL_SHOW_HANDLE = os.getenv("SHOW_HANDLE", "true").strip().lower() == "true"

# A WATERMARK, not a label. It used to be near-opaque white type (alpha 235)
# on a dark rounded plate, parked in the middle of the lower third -- which
# reads as part of the content rather than as ownership, and competes with the
# caption it sits under. Low alpha, no plate, and out of the caption's way.
# 0-255; below about 60 it disappears on bright footage, above about 160 it
# starts reading as content again.
REEL_HANDLE_ALPHA = int(os.getenv("REEL_HANDLE_ALPHA", "105"))
# Backing plate behind the handle. Off: a plate is what made it look stuck on.
REEL_HANDLE_PLATE = os.getenv("REEL_HANDLE_PLATE", "false").strip().lower() == "true"
INSTAGRAM_HANDLE = BRAND_HANDLE
REEL_END_CARD_SECONDS = CTA_SECONDS
REEL_FOLLOW_TEXT = CTA_CARD_TEXT
REEL_TTS_ENGINE = TTS_ENGINE
REEL_EDGE_TTS_VOICE = EDGE_TTS_VOICE
REEL_EDGE_TTS_RATE = EDGE_TTS_RATE
REEL_TTS_MODEL = TTS_MODEL
REEL_VOICE_SAMPLE = VOICE_SAMPLE
REEL_EXAGGERATION = EXAGGERATION
REEL_CFG_WEIGHT = CFG_WEIGHT
REEL_TEMPERATURE = TEMPERATURE
REEL_SPEECH_SPEED = SPEECH_SPEED
REEL_ASR_BACKEND = ASR_BACKEND
REEL_ASR_MODEL = ASR_MODEL
REEL_ASR_LANGUAGE = ASR_LANGUAGE
REEL_REJECT_FACES = REJECT_FACES
