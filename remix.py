"""Remixes a YouTube skill/tutorial clip into a reel: trimmed to its one
useful moment, re-narrated in our own voice, and credited everywhere a credit
can go -- never a blind repost of someone else's video.

Two modes, same mechanics:
  skill -- `query` is a skill/technique keyword, searched directly.
  topic -- `query` is still the search keyword, but the script nods to
           today's countdown topic (`--topic`) so the two reels feel like a
           matched pair rather than unrelated posts on the same day.

Three Gemini calls, cached like planner.py's (only `refresh` spends quota):
  pick_video      -- which search result actually demonstrates something
  locate_segment  -- which moment in THAT video is worth excerpting
  write_script    -- our own narration describing it

The renderer needs no changes at all: videocomposite.assemble() already never
carries a clip's own audio, only the narration track, so the source's actual
words are never what plays. See sources/youtube.py's "Clip remix" section for
why this is a different, looser posture than the countdown's Creative-Commons
gate, and why the credit (on screen, in the voice, in the caption, in the
audit log) is mandatory rather than cosmetic.
"""
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

import config
import narrate
import videocomposite
from planner import PlannerError, QuotaExhausted
from sources import youtube

SCHEMA_VERSION = 3


class RemixError(RuntimeError):
    pass


# Skillificationed is Telugu/Indian-audience only for now (2026-10-01) -- see
# config.ASR_LANGUAGE, the same switch narrate.py and spec.py already key off
# of. Framing is shared across all three prompts below so judgment on
# ambiguous candidates, not just the final script, accounts for the audience.
_AUDIENCE_NOTE = (
    "This channel's audience is Indian, primarily Telugu-speaking. Prefer "
    "techniques, framing and context that resonate with Indian daily life; "
    "if the source video assumes something uncommon in India (a Western "
    "household item, imperial units, a product not sold here), adapt the "
    "framing or note the Indian equivalent rather than assuming it applies "
    "directly.")

# Gemini has never been asked to write Telugu anywhere else in this codebase
# -- planner.py is English-only by design (hand-written specs carry the
# existing Telugu countdown reels instead) -- so this is new territory and
# the output deserves a native-speaker sanity check on the first several
# builds, same as RUNBOOK documents for the hand-written Telugu specs.
_TELUGU_SCRIPT_RULES = """Write hook, beats, and cta in natural, CONVERSATIONAL Telugu -- how a person actually talks, not textbook or overly formal Telugu. TRANSLITERATE (write in Telugu script, keep the actual word) any skill/technique/brand/tool name nobody would recognise if translated -- do not force a dictionary translation of a named thing. Code-switching a proper noun or a common English loanword into the Telugu sentence is normal and expected, the way people actually speak."""


def _check_transliteration(text: str, where: str) -> None:
    """Catches a forced dictionary translation of a named thing -- the same
    rule spec.py enforces for hand-written Telugu specs (config.
    TRANSLITERATION_DENYLIST), applied here because this is the first place
    in the codebase where Gemini itself writes Telugu rather than a human."""
    for forced, correct in config.TRANSLITERATION_DENYLIST.items():
        if forced in text:
            raise RemixError(
                f"{where}: {forced!r} is a forced dictionary translation, "
                f"not what this audience calls it -- use {correct!r} "
                f"instead. Full text: {text!r}")


# --- Gemini plumbing, same retry policy as planner._ask ---------------------

def _ask(prompt: str, schema: dict, temperature: float = 0.6,
        max_retries: int = 2) -> dict:
    if not config.GEMINI_API_KEY:
        raise PlannerError("GEMINI_API_KEY is not set in .env")
    client = genai.Client(api_key=config.GEMINI_API_KEY)
    for attempt in range(max_retries + 1):
        try:
            response = client.models.generate_content(
                model=config.GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=schema,
                    temperature=temperature),
            )
            return json.loads(response.text)
        except genai_errors.ServerError as exc:
            if attempt == max_retries:
                raise PlannerError(f"Gemini unavailable: {str(exc)[:120]}") from exc
            time.sleep(5 * (attempt + 1))
        except genai_errors.ClientError as exc:
            message = str(exc)
            if "RESOURCE_EXHAUSTED" not in message and "429" not in message:
                raise
            delay = re.search(r"retryDelay['\"]?:\s*['\"]?(\d+)s", message)
            if "PerDay" in message or not delay or attempt == max_retries:
                raise QuotaExhausted(
                    "Gemini free-tier quota exhausted. Cached remix plans "
                    "still build offline.") from exc
            time.sleep(int(delay.group(1)) + 2)
    raise QuotaExhausted("Gemini quota exhausted")


def _cache_key(*parts) -> str:
    raw = "|".join(str(p) for p in parts) + f"|remixv{SCHEMA_VERSION}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _cached(key: str):
    path = config.PLAN_CACHE / f"{key}.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except ValueError:
            return None
    return None


def _store(key: str, payload: dict) -> None:
    try:
        config.PLAN_CACHE.mkdir(parents=True, exist_ok=True)
        (config.PLAN_CACHE / f"{key}.json").write_text(
            json.dumps(payload, indent=2, default=str))
    except OSError:
        pass


# --- 1. pick the video -------------------------------------------------------

_PICK_SCHEMA = {
    "type": "object",
    "properties": {
        "video_id": {"type": "string",
                    "description": "copied exactly from the candidate list"},
        "why": {"type": "string"},
    },
    "required": ["video_id", "why"],
}

_PICK_PROMPT = """You are picking ONE YouTube video for a short-form skill/tutorial reel on {brand}, an account that teaches one skill in under a minute, built on a credited excerpt of someone else's demonstration.

{audience}

{context}

Candidates:
{listing}

Pick the ONE video that best fits. Judge by:
* It actually DEMONSTRATES a skill or technique visually -- not a talking head, not a vlog, not a reaction.
* A single clear moment within it could be understood in 10-20 seconds on its own, out of context.
* Prefer a creator who clearly knows what they are doing over a low-effort upload.
* Skip anything that looks like ITSELF a repost or compilation of someone else's footage -- crediting a reposter credits the wrong person.
* STRONGLY prefer a candidate marked [captions: yes] over one marked [captions: no], all else being close -- the next step picks the exact moment to excerpt from the transcript, and a video with no captions is picked almost blind. Only choose a no-captions candidate if it is clearly the better video.

Return JSON: video_id (copied exactly) and why (one sentence).
"""


def pick_video(query: str, mode: str, topic: str = "", top_n: int | None = None,
              refresh: bool = False, log=None) -> dict:
    """Searches YouTube for `query`, drops recently-remixed videos, and asks
    Gemini which result is actually worth excerpting. `log`, when given, is
    shown the full candidate pool BEFORE Gemini picks -- so a human can judge
    whether the field Gemini chose from was worth choosing from at all,
    before anything downloads or spends a Sarvam credit."""
    excluded = youtube.recent_video_ids()
    all_candidates = youtube.search_topic(query, top_n) or []
    candidates = [c for c in all_candidates if c["video_id"] not in excluded]
    if log:
        log(f"\n  searched '{query}': {len(all_candidates)} result(s), "
            f"{len(all_candidates) - len(candidates)} excluded "
            f"(remixed within the last {config.REMIX_REUSE_DAYS} days)")
        for c in candidates:
            dur = f"{c['duration']:.0f}s" if c.get("duration") else "?s"
            views = f"{c['view_count']:,}" if c.get("view_count") else "? views"
            caps = "captions" if c.get("has_captions") else "NO captions"
            log(f"    [{c['video_id']}] {c['title'][:70]}  ({caps})")
            log(f"        {c['channel']} — {dur}, {views} — {c['url']}")
    if not candidates:
        raise RemixError(
            f"no fresh candidates for '{query}' "
            f"({len(excluded)} recently-remixed video(s) excluded)")

    key = _cache_key("pick", query, mode, topic,
                     tuple(c["video_id"] for c in candidates))
    if not refresh:
        hit = _cached(key)
        if hit:
            if log:
                log(f"\n  (picker cached — no Gemini quota spent)")
            return hit

    context = (f"This reel pairs with today's countdown topic: {topic!r} -- "
              f"a natural companion piece, not a forced match."
              if mode == "topic" and topic else
              f"Standalone skill reel. Search keyword: {query!r}.")
    listing = "\n".join(
        f"[{c['video_id']}] {c['title']} (channel: {c['channel']}, "
        f"{c['duration'] if c['duration'] else '?'}s, "
        f"{c.get('view_count') or '?'} views, "
        f"captions: {'yes' if c.get('has_captions') else 'no'})"
        for c in candidates)
    result = _ask(_PICK_PROMPT.format(
        brand=config.BRAND_HANDLE or "this channel", audience=_AUDIENCE_NOTE,
        context=context, listing=listing), _PICK_SCHEMA, temperature=0.4)
    winner = next((c for c in candidates
                   if c["video_id"] == result.get("video_id")), None)
    if winner is None:
        raise RemixError(f"picker returned unknown id {result.get('video_id')!r}")
    picked = {**winner, "why": result.get("why", "")}
    if log:
        log(f"\n  picked [{winner['video_id']}] {winner['title']} "
            f"— {winner['channel']}\n  why: {picked['why']}")
    _store(key, picked)
    return picked


# --- 2. locate the key moment ------------------------------------------------

_SEGMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "start_s": {"type": "number"},
        "end_s": {"type": "number"},
        "on_screen_label": {"type": "string",
                            "description": "max 6 words, what this moment shows"},
        "reason": {"type": "string"},
    },
    "required": ["start_s", "end_s", "on_screen_label", "reason"],
}

_SEGMENT_PROMPT = """A tutorial/skill video is being excerpted for a short-form reel. Pick the SINGLE most useful, self-contained moment -- the part that actually shows the technique, never the intro, the sign-off, or a sponsor read.

{audience}

Title: {title}
Channel: {channel}
Duration: {duration:.0f}s
Chapters: {chapters}
Auto-caption transcript (approximate timing, may be imperfect):
{transcript}

Pick start_s and end_s (seconds into the video) for a segment between {min_s:.0f} and {max_s:.0f} seconds long that captures ONE complete, understandable moment of the technique -- something that makes sense even without the rest of the video. on_screen_label: max 6 words naming what it shows. reason: one sentence on why this moment specifically.
"""


def locate_segment(video: dict, mode: str, topic: str = "",
                   refresh: bool = False, log=None) -> dict:
    info = youtube.video_info(video["video_id"])
    key = _cache_key("segment", video["video_id"], mode, topic)
    if not refresh:
        hit = _cached(key)
        if hit:
            if log:
                log(f"  (segment cached — no Gemini quota spent)")
            return {**hit, "video_info": info}

    chapters = "; ".join(
        f"{c.get('start_time', 0):.0f}s {c.get('title', '')}"
        for c in (info.get("chapters") or [])) or "(none)"
    text = youtube.transcript(video["video_id"])
    if log:
        log(f"  chapters: {chapters}")
        log(f"  transcript: {'(none available)' if not text else f'{len(text)} chars fetched'}")
    result = _ask(_SEGMENT_PROMPT.format(
        audience=_AUDIENCE_NOTE, title=info["title"], channel=info["channel"],
        duration=info["duration"] or 60.0, chapters=chapters,
        transcript=text or "(no captions available)",
        min_s=config.REMIX_CLIP_MIN_SECONDS,
        max_s=config.REMIX_CLIP_MAX_SECONDS), _SEGMENT_SCHEMA, temperature=0.3)

    duration = info["duration"] or (float(result.get("end_s") or 20) + 5)
    start = max(0.0, min(float(result.get("start_s") or 0),
                         max(duration - config.REMIX_CLIP_MIN_SECONDS, 0)))
    length = max(config.REMIX_CLIP_MIN_SECONDS,
                min(float(result.get("end_s") or 0) - start,
                    config.REMIX_CLIP_MAX_SECONDS))
    end = min(start + length, duration)
    segment = {"start_s": round(start, 1), "end_s": round(end, 1),
              "on_screen_label": (result.get("on_screen_label") or "")[:60],
              "reason": result.get("reason", "")}
    _store(key, segment)
    return {**segment, "video_info": info}


# --- 3. write our own narration ----------------------------------------------

_SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "hook": {"type": "string"},
        "beats": {"type": "array", "items": {"type": "string"},
                 "minItems": 1, "maxItems": 3},
        "cta": {"type": "string",
               "description": "spoken closing line, unique to this video"},
        "cover_text": {"type": "string"},
        "caption": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"},
                     "minItems": 5, "maxItems": 5},
    },
    "required": ["hook", "beats", "cta", "cover_text", "caption", "hashtags"],
}

_SCRIPT_PROMPT = """You are writing the voiceover for {brand}'s short-form reel. Format: a {seconds:.0f}-second excerpt of someone else's skill/tutorial video plays (muted, looping if needed) while YOUR voice narrates over it. The viewer has to learn something real in that time.

{audience}

The clip: "{title}", by {channel}.
What this moment shows: {on_screen_label} -- {reason}
{context}

Write ORIGINAL commentary in your own words -- never read or paraphrase the creator's own script; explain the technique the way a teacher would, as your own take on their demonstration. Never claim the technique or the footage as your own.
{language_rules}

JSON:
* hook: what the voice says in the first 2-3 seconds, max 14 words. Names the skill, a pattern interrupt, never a greeting.
* beats: 1-3 short lines (max 20 words each) explaining what is shown and why it works, or the one detail that matters. Present tense, plain spoken prose, no emojis or markdown.
* cta: the voice's closing line, max 14 words, SPECIFIC to this skill -- not a generic "share this" but something that earns a send/save/comment because of what THIS video just showed (e.g. naming who would need this trick, or the specific moment it saves them). One unique line per video, never a template.
* cover_text: burned on screen for the whole reel, max 6 words, sentence case.
* caption: Instagram caption{caption_language}. Line 1: one emoji + a punchy one-liner. Then 1-2 short sentences on why this is worth knowing. Then a question inviting comments. No credit line -- that is added automatically afterward.
* hashtags: exactly 5, specific first then broad, each starting with '#'. Keep hashtags in English/Romanized form regardless of the caption's language -- that is how they get discovered.
"""


def write_script(video: dict, segment: dict, mode: str, topic: str = "",
                 refresh: bool = False) -> dict:
    info = segment["video_info"]
    key = _cache_key("script", video["video_id"], segment["start_s"],
                     segment["end_s"], mode, topic)
    if not refresh:
        hit = _cached(key)
        if hit:
            return hit
    context = (f"This pairs with today's '{topic}' reel -- nod to it only if "
              f"it fits naturally, never forced." if mode == "topic" and topic
              else "")
    telugu = config.ASR_LANGUAGE == "te"
    result = _ask(_SCRIPT_PROMPT.format(
        brand=config.BRAND_HANDLE or "this channel", audience=_AUDIENCE_NOTE,
        seconds=segment["end_s"] - segment["start_s"],
        title=info["title"], channel=info["channel"],
        on_screen_label=segment["on_screen_label"], reason=segment["reason"],
        context=context,
        language_rules=_TELUGU_SCRIPT_RULES if telugu else "",
        caption_language=" -- written in Telugu" if telugu else ""),
        _SCRIPT_SCHEMA, temperature=0.7)

    if telugu:
        _check_transliteration(result.get("hook", ""), "hook")
        for i, beat in enumerate(result.get("beats") or []):
            _check_transliteration(beat, f"beat {i + 1}")
        _check_transliteration(result.get("cta", ""), "cta")

    tags = []
    for tag in result.get("hashtags", []):
        t = str(tag).strip().replace(" ", "")
        if t:
            tags.append(t if t.startswith("#") else "#" + t)
    result["hashtags"] = tags[:5]
    if not (result.get("cta") or "").strip():
        result["cta"] = config.REMIX_CTA_LINE_BY_LANGUAGE.get(
            config.ASR_LANGUAGE, config.REMIX_CTA_LINE)
    _store(key, result)
    return result


# --- orchestration ------------------------------------------------------------

def plan(query: str, mode: str = "skill", topic: str = "",
        refresh: bool = False, log=None) -> dict:
    """The full remix plan: picked video, located segment, written script.
    Cached at each step; only `refresh` spends Gemini quota. Written to
    output/remix-<slug>/remix_plan.json by the CLI -- edit it freely before
    `remix-build`, same convention as a countdown's plan.json.

    `log`, when given, is shown the FULL candidate pool and the segment's
    chapters/transcript -- not just the final pick -- so a human can judge
    whether the source material is actually worth excerpting before anything
    downloads or a Sarvam credit is spent on narration.
    """
    mode = mode if mode in ("skill", "topic") else "skill"
    video = pick_video(query, mode, topic, refresh=refresh, log=log)
    segment = locate_segment(video, mode, topic, refresh=refresh, log=log)
    script = write_script(video, segment, mode, topic, refresh=refresh)
    info = segment["video_info"]
    return {
        "query": query, "mode": mode, "topic": topic,
        "video_id": video["video_id"], "title": info["title"],
        "channel": info["channel"], "channel_url": info["channel_url"],
        "video_url": info["url"], "license": info.get("license", ""),
        "why_picked": video.get("why", ""),
        "segment": {"start_s": segment["start_s"], "end_s": segment["end_s"],
                   "on_screen_label": segment["on_screen_label"],
                   "reason": segment["reason"]},
        "hook": script["hook"], "beats": script["beats"], "cta": script["cta"],
        "cover_text": script["cover_text"], "caption": script["caption"],
        "hashtags": script["hashtags"],
    }


def build(plan_data: dict, day_dir: Path, log, tag: str = "remix") -> dict:
    day_dir.mkdir(parents=True, exist_ok=True)
    video_id = plan_data["video_id"]
    seg = plan_data["segment"]
    log(f"\n  downloading segment {seg['start_s']:.0f}s-{seg['end_s']:.0f}s "
        f"of {video_id} ({plan_data['channel']})")
    clip = youtube.download_segment(video_id, seg["start_s"], seg["end_s"],
                                    day_dir / "source")
    clip_duration = videocomposite.probe_duration(clip)
    log(f"    downloaded {clip_duration:.1f}s")

    # The credit beat is CODE, never the model's: the one line here that must
    # be exactly right names a real person, the same reason build.py's
    # countdown numbers are spoken by the pipeline and never written by Gemini.
    credit_template = config.REMIX_CREDIT_LINE_BY_LANGUAGE.get(
        config.ASR_LANGUAGE, config.REMIX_CREDIT_LINE_EN)
    credit_line = credit_template.format(channel=plan_data["channel"])
    beats = [{"role": "hook", "line": plan_data["hook"], "rank": 0}]
    for line in plan_data["beats"]:
        beats.append({"role": "beat", "line": line, "rank": 0})
    beats.append({"role": "credit", "line": credit_line, "rank": 0})
    beats.append({"role": "cta", "line": plan_data["cta"], "rank": 0})
    beats = [b for b in beats if b["line"].strip()]
    script_text = " ".join(b["line"] for b in beats)

    log("\n  voicing (one beat at a time):")
    voice = narrate.narrate(beats, day_dir, log, tag)
    audio, duration = voice["audio"], voice["duration"]
    spans, timed = voice["spans"], voice["words"]
    log(f"    {duration:.1f}s total, {len(timed)} words timed")

    credits = ([f"{plan_data['title']} — {plan_data['channel']} / YouTube"]
              if config.CREDITS_CARD else [])

    pages = videocomposite.plan_captions(timed, duration,
                                         fallback_text=script_text,
                                         beat_spans=spans)
    videocomposite.write_srt(pages, day_dir / f"{tag}.srt")
    frames_dir = day_dir / f"{tag}_frames"
    # No countdown items here -- items=[] means no rank card/chip, just
    # captions, the follow card, and the credits card.
    _, frame_count = videocomposite.render_caption_track(
        pages, duration, frames_dir, items=[], credits=credits)
    log(f"    {len(pages)} caption pages, {frame_count} frames, "
        f"{len(credits)} credit line(s)")

    clips = [clip]
    beat_clips = [[0] for _ in beats]
    durations = [clip_duration]
    segments = videocomposite.build_segments_multi(spans, beat_clips, durations,
                                                    duration)
    log(f"    {len(segments)} shot(s) from the one sourced clip "
        f"(looped to cover {duration:.1f}s of narration)")

    out = videocomposite.assemble(clips, audio, segments, frames_dir,
                                  day_dir / f"{tag}.mp4", duration)
    log(f"    -> {out.name} ({out.stat().st_size / 1_048_576:.1f} MB, "
        f"{duration + 0.35:.1f}s)")

    youtube.record_used(video_id, plan_data["title"])
    youtube.log_credit({
        "logged_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "slug": day_dir.name, "mode": plan_data["mode"],
        "query_or_topic": plan_data.get("topic") or plan_data["query"],
        "video_id": video_id, "title": plan_data["title"],
        "channel": plan_data["channel"], "channel_url": plan_data["channel_url"],
        "video_url": plan_data["video_url"],
        "segment_start_s": seg["start_s"], "segment_end_s": seg["end_s"],
        "segment_seconds": round(seg["end_s"] - seg["start_s"], 1),
        "license": plan_data.get("license", ""),
        "disclosure": config.REMIX_DISCLOSURE_LINE,
        "output_file": out.name,
    })

    manifest = {
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": plan_data["mode"], "topic": plan_data.get("topic", ""),
        "query": plan_data["query"], "title": plan_data["cover_text"],
        "source": {"video_id": video_id, "title": plan_data["title"],
                   "channel": plan_data["channel"],
                   "channel_url": plan_data["channel_url"],
                   "video_url": plan_data["video_url"],
                   "segment_start_s": seg["start_s"],
                   "segment_end_s": seg["end_s"],
                   "why_picked": plan_data.get("why_picked", "")},
        "script": script_text, "duration": round(duration + 0.35, 2),
        "words": len(script_text.split()), "credits": credits,
        "file": out.name,
    }
    (day_dir / f"{tag}.json").write_text(json.dumps(manifest, indent=2, default=str))

    parts = [plan_data["caption"],
            f"🎥 via {plan_data['channel']} on YouTube",
            f"Original: {plan_data['video_url']}"]
    if plan_data.get("hashtags"):
        parts.append(" ".join(plan_data["hashtags"]))
    parts.append(config.REMIX_DISCLOSURE_LINE)
    (day_dir / f"{tag}.caption.txt").write_text(
        "\n\n".join(p for p in parts if p))

    return manifest
