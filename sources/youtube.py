"""YouTube, behind a harder gate than Pexels or Commons: a genuinely verified
Creative Commons license, checked per-video against YouTube's own license
flag -- never trusted from a title.

Searching "Ganesh temple aerial creative commons" turned up dozens of videos
titled "FREE TO USE", "NO COPYRIGHT", "Free Stock Footage" -- and every one of
those specific self-declared claims carried YouTube's default Standard
license when actually checked. A title is not a license; it is frequently
someone else's footage re-uploaded with a misleading label, which offers no
real protection and would put this project's own honesty-about-footage ethos
exactly backwards. Exactly one video in that search batch carried an actual
"Creative Commons Attribution license (reuse allowed)" flag -- confirming the
check itself works, and that real matches are rare, not that the check is
broken.

Two ways in:

  fetch_by_id   the only path that can ever produce footage. Mirrors
                stock.pinned_shots: a person watched the actual video and is
                choosing it by id and timestamp, the same accountability a
                Pexels pin carries. Refuses, loudly, anything whose license
                is not exactly Creative Commons -- never a silent fallback to
                unlicensed use.

  search        an unauthenticated `ytsearch:` scrape (yt-dlp, no API key) --
                a discovery aid ONLY, for a human to screen candidates before
                pinning one by id. Never auto-accepted; most results will
                have no usable license at all, as above.

  search_api    the real fix, once config.YOUTUBE_API_KEY exists: the YouTube
                Data API's search.list has an actual videoLicense=
                creativeCommon filter, server-side and reliable, unlike
                guessing from scraped titles. Free tier: 100 units per search,
                10,000/day. Falls back to nothing (not an error) without a
                key, since the free `search()` path above still works without
                one.
"""
import csv
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

import config

_TIMEOUT = 30
_API_SEARCH = "https://www.googleapis.com/youtube/v3/search"
# pip installs the yt-dlp console-script next to whichever python installed
# it; a bare "yt-dlp" on PATH may resolve to a different (or no) interpreter's
# copy, so resolve the sibling of THIS venv's python first.
_YT_DLP = str(Path(sys.executable).parent / "yt-dlp")


class YouTubeError(RuntimeError):
    pass


def _yt_dlp(*args: str, timeout: int = 60) -> str:
    result = subprocess.run(
        [_YT_DLP, *args], capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise YouTubeError(f"yt-dlp failed: {result.stderr.strip()[-500:]}")
    return result.stdout


def _is_creative_commons(info: dict) -> bool:
    license_ = (info.get("license") or "").strip().lower()
    return "creative commons" in license_


def search(query: str, limit: int = 10) -> list:
    """Discovery only -- candidates for a HUMAN to watch and screen.

    No API key needed (scrapes YouTube's own search page via yt-dlp), so this
    always works, but most results carry no real license -- that is expected,
    not a bug. Never treat a result here as usable footage; find the id you
    actually watched and trust, then pin it with fetch_by_id.
    """
    try:
        raw = _yt_dlp(f"ytsearch{limit}:{query}", "--dump-json", "--no-download",
                      "--skip-download", timeout=90)
    except YouTubeError:
        return []
    results = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            info = json.loads(line)
        except ValueError:
            continue
        results.append({
            "id": info.get("id"),
            "title": info.get("title"),
            "duration": info.get("duration"),
            "uploader": info.get("uploader"),
            "url": info.get("webpage_url") or f"https://youtu.be/{info.get('id')}",
            "license": info.get("license"),
            "creative_commons": _is_creative_commons(info),
        })
    return results


def search_api(query: str, max_results: int = 10) -> list:
    """The reliable version of search(): YouTube Data API's own CC filter.

    Requires config.YOUTUBE_API_KEY (a free API key -- no OAuth, no app
    review, just a key from Google Cloud Console). Returns an empty list
    (not an error) with no key set, so callers can try this first and fall
    back to search() without special-casing.
    """
    if not config.YOUTUBE_API_KEY:
        return []
    params = {
        "part": "snippet", "q": query, "type": "video",
        "videoLicense": "creativeCommon", "maxResults": max_results,
        "key": config.YOUTUBE_API_KEY,
    }
    response = requests.get(_API_SEARCH, params=params, timeout=_TIMEOUT)
    if response.status_code != 200:
        raise YouTubeError(f"YouTube Data API search failed: {response.text[:300]}")
    items = response.json().get("items", [])
    return [{
        "id": item["id"]["videoId"],
        "title": item["snippet"]["title"],
        "uploader": item["snippet"]["channelTitle"],
        "url": f"https://youtu.be/{item['id']['videoId']}",
        "creative_commons": True,  # the API filter already guarantees this
    } for item in items]


def fetch_by_id(video_id: str, start_seconds: float = 0.0,
                clip_seconds: float = 6.0) -> tuple:
    """Downloads exactly `clip_seconds` of one YouTube video, id-pinned.

    Raises YouTubeError if the video is not actually Creative Commons --
    this is the one gate that can never be bypassed, whatever the title says.
    `start_seconds` is the point IN THE SOURCE VIDEO the human who watched it
    chose; there is no way to guess a good moment automatically.
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    info_raw = _yt_dlp(url, "--dump-json", "--no-download", timeout=60)
    info = json.loads(info_raw)
    if not _is_creative_commons(info):
        raise YouTubeError(
            f"{video_id} ('{info.get('title', '')[:60]}') is not Creative "
            f"Commons licensed (license={info.get('license')!r}) -- refusing "
            "to use it. A 'no copyright' or 'free to use' TITLE is not a "
            "license; only YouTube's own CC flag counts.")

    config.ASSET_CACHE.mkdir(parents=True, exist_ok=True)
    out_path = config.ASSET_CACHE / f"youtube-{video_id}-{start_seconds:.0f}-{clip_seconds:.0f}.mp4"
    if out_path.exists():
        pass  # already downloaded and trimmed
    else:
        end_seconds = start_seconds + clip_seconds
        raw_path = out_path.with_suffix(".raw.mp4")
        _yt_dlp(
            url, "-f", "bv*[height<=1080]+ba/b[height<=1080]",
            "--merge-output-format", "mp4",
            "--download-sections", f"*{start_seconds:.0f}-{end_seconds:.0f}",
            "-o", str(raw_path), timeout=180)
        result = subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-i", str(raw_path), "-t", f"{clip_seconds:.2f}",
             "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "20",
             str(out_path)],
            capture_output=True, text=True, timeout=120)
        raw_path.unlink(missing_ok=True)
        if result.returncode != 0:
            out_path.unlink(missing_ok=True)
            raise YouTubeError(f"could not trim {video_id}: {result.stderr.strip()[-300:]}")

    credit = {
        "source": "youtube", "asset_id": f"youtube-{video_id}",
        "page": info.get("webpage_url") or url,
        "author": info.get("uploader") or info.get("channel") or "",
        # "licence" (not "license") and "attribution_required" match
        # sources/commons.py's spelling -- build.credit_lines() keys off
        # exactly these two fields to decide what earns a line on the
        # closing credits card. Every Creative Commons license this module
        # will accept requires attribution; there is no CC0/public-domain
        # case here the way Commons sometimes has.
        "licence": info.get("license") or "Creative Commons",
        "attribution_required": True,
        "match_ratio": "pinned",
    }
    return out_path, credit


# === Clip remix (commentary reels) ==========================================
# Everything above this line proves a Creative Commons licence before a frame
# of YouTube footage can carry a countdown item -- that gate stays exactly as
# strict as it was.
#
# This section is a DIFFERENT, DELIBERATELY SEPARATE posture: a short excerpt
# of a skill/tutorial video, re-narrated in our own voice (the renderer never
# carries the source's own audio -- see videocomposite.assemble, which maps
# only the narration track), trimmed to its one useful moment, and credited
# on screen, in the spoken narration, in the Instagram caption, AND in a
# permanent audit log -- never the CC check above, because most tutorial
# creators never set that flag regardless of how reusable their video
# actually is in spirit.
#
# This is commentary/review-style reuse (transformative: new narration, a
# short excerpt, never the creator's own words), not a licence claim. It is
# NOT the same legal footing as fetch_by_id's verified-CC path above, and the
# audit log exists so a takedown or a rights inquiry can be answered quickly
# and honestly -- it documents what was used and credits who made it, it does
# not grant permission. Keep clips short (REMIX_CLIP_MAX_SECONDS) and the
# credit mandatory; both are load-bearing, not cosmetic.
LEDGER = config.STATE_DIR / "youtube_remix_ledger.json"
CREDIT_LOG = config.STATE_DIR / "youtube_credit_log.csv"
_LEDGER_KEEP = 1000
_CREDIT_LOG_FIELDS = [
    "logged_at_utc", "slug", "mode", "query_or_topic", "video_id", "title",
    "channel", "channel_url", "video_url", "segment_start_s", "segment_end_s",
    "segment_seconds", "license", "disclosure", "output_file",
]


def _has_english_captions(info: dict) -> bool:
    """en OR a regional tag (en-IN, en-US, ...) in either auto or manual
    captions. See transcript()'s docstring -- en-IN is the common case for an
    Indian creator and an exact "en" check misses it entirely."""
    langs = {*((info.get("automatic_captions") or {}).keys()),
            *((info.get("subtitles") or {}).keys())}
    return any(lang == "en" or lang.startswith("en-") for lang in langs)


def search_topic(query: str, limit: int | None = None) -> list:
    """Keyword harvest for the remix pipeline -- full per-video metadata
    (NOT --flat-playlist), because locate_segment's pick is far more reliable
    when a transcript exists, so pick_video needs to know up front which
    candidates actually have one (has_captions) rather than discovering it
    only after a video is already chosen. Slower than a flat listing (~1.5s a
    candidate) but still free -- no Gemini or Sarvam call happens here."""
    limit = limit or config.REMIX_SEARCH_RESULTS
    try:
        raw = _yt_dlp(f"ytsearch{limit}:{query}", "--dump-json", "--no-download",
                      "--skip-download", timeout=180)
    except YouTubeError as exc:
        print(f"  ! youtube search failed: {exc}")
        return []
    out = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            info = json.loads(line)
        except ValueError:
            continue
        vid = info.get("id")
        if not vid:
            continue
        out.append({
            "video_id": vid,
            "title": (info.get("title") or "").strip(),
            "channel": info.get("channel") or info.get("uploader") or "",
            "channel_url": info.get("channel_url") or info.get("uploader_url") or "",
            "duration": info.get("duration"),
            "view_count": info.get("view_count"),
            "url": info.get("webpage_url") or f"https://www.youtube.com/watch?v={vid}",
            "has_captions": _has_english_captions(info),
        })
    return out


def video_info(video_id: str) -> dict:
    """Full metadata for one video: description, chapters, licence, duration.

    Chapters (when a creator sets them) are the single best signal for where
    a tutorial's "key moment" sits -- far more reliable than guessing from a
    transcript alone -- so they are surfaced here even though search_topic's
    flat listing cannot see them.
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    raw = _yt_dlp(url, "--dump-json", "--no-download", timeout=60)
    info = json.loads(raw)
    return {
        "video_id": video_id,
        "title": info.get("title") or "",
        "description": info.get("description") or "",
        "channel": info.get("channel") or info.get("uploader") or "",
        "channel_url": info.get("channel_url") or info.get("uploader_url") or "",
        "duration": float(info.get("duration") or 0),
        "chapters": info.get("chapters") or [],
        "license": info.get("license") or "",
        "url": info.get("webpage_url") or url,
    }


_VTT_TAG = re.compile(r"<[^>]+>")
_VTT_CUE = re.compile(r"(\d\d:\d\d:\d\d\.\d\d\d) --> ")


def _parse_vtt(text: str) -> list:
    """Auto-caption cues as [{start_s, text}], collapsed from YouTube's
    rolling-caption format (each cue repeats the previous line and grows it
    by a few words) down to one entry per completed phrase: a cue is dropped
    when its text is a prefix of the next cue's, which keeps only the final,
    longest version of each growing line."""
    cues, current = [], None
    for line in text.splitlines():
        m = _VTT_CUE.match(line)
        if m:
            h, mi, s = m.group(1).split(":")
            current = int(h) * 3600 + int(mi) * 60 + float(s)
            continue
        clean = _VTT_TAG.sub("", line).strip()
        if not clean or clean.startswith(("WEBVTT", "Kind:", "Language:")):
            continue
        if current is None:
            continue
        if cues and cues[-1]["text"] == clean:
            continue
        cues.append({"start_s": current, "text": clean})
    collapsed = []
    for i, cue in enumerate(cues):
        nxt = cues[i + 1]["text"] if i + 1 < len(cues) else ""
        if nxt.startswith(cue["text"]) and nxt != cue["text"]:
            continue
        collapsed.append(cue)
    return collapsed


def transcript(video_id: str, max_chars: int = 6000) -> str:
    """Auto-caption transcript as 'Ns: phrase' lines, for Gemini to read when
    picking the clip's key moment. Never raises -- most videos have auto
    captions, but a missing/disabled track means an empty string, and segment
    picking falls back to the title/description/chapters alone.

    "en.*", not "en" -- an Indian creator's auto-captions are almost always
    tagged en-IN, not plain en, and yt-dlp's exact-match --sub-lang silently
    returns nothing for those (confirmed: Venkatesh Bhat's filter-coffee video
    carries en-IN captions that plain "en" missed entirely, 2026-10-01). The
    wildcard also matches en-US/en-GB/etc. on a non-Indian source video.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "cap"
        try:
            _yt_dlp(f"https://www.youtube.com/watch?v={video_id}",
                   "--skip-download", "--write-auto-sub", "--sub-lang", "en.*",
                   "--sub-format", "vtt", "-o", str(out), timeout=60)
        except YouTubeError:
            # A second matching language variant (e.g. both en-IN and en-US
            # exist) can fail after the first already wrote successfully --
            # check disk before giving up on the whole call.
            pass
        vtt_files = sorted(Path(tmp).glob("cap*.vtt"))
        if not vtt_files:
            return ""
        cues = _parse_vtt(vtt_files[0].read_text(errors="replace"))
    lines, total = [], 0
    for cue in cues:
        line = f"{cue['start_s']:.0f}s: {cue['text']}"
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines)


def download_segment(video_id: str, start: float, end: float,
                     out_dir: Path, pad: float = 0.6) -> Path:
    """Downloads ONLY the [start, end] window (plus a little padding so a cut
    does not land mid-word of someone's sentence), never the full video.

    No licence check here -- see the module docstring above this section.
    Caller is responsible for the segment length (config.REMIX_CLIP_MAX_SECONDS)
    and for crediting the result.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    s = max(0.0, start - pad)
    e = end + pad
    out_path = out_dir / f"remix-{video_id}-{int(s)}-{int(e)}.mp4"
    if out_path.exists() and out_path.stat().st_size > 0:
        return out_path
    url = f"https://www.youtube.com/watch?v={video_id}"
    raw_path = out_path.with_suffix(".raw.mp4")
    _yt_dlp(url, "-f", "bv*[height<=1080]+ba/b[height<=1080]",
           "--merge-output-format", "mp4",
           "--download-sections", f"*{s:.1f}-{e:.1f}",
           "-o", str(raw_path), timeout=300)
    if not raw_path.exists() or raw_path.stat().st_size == 0:
        raise YouTubeError(f"segment download produced nothing for {video_id}")
    raw_path.replace(out_path)
    return out_path


# --- reuse ledger: don't remix the same video twice inside the window -------

def _load_ledger() -> list:
    try:
        return json.loads(LEDGER.read_text()).get("used") or []
    except (OSError, ValueError):
        return []


def recent_video_ids(days: int | None = None) -> set:
    cutoff = time.time() - (days if days is not None else config.REMIX_REUSE_DAYS) * 86400
    return {e["video_id"] for e in _load_ledger() if e.get("at", 0) >= cutoff}


def record_used(video_id: str, title: str) -> None:
    used = _load_ledger()
    used.append({"video_id": video_id, "title": title[:160], "at": int(time.time())})
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps({"used": used[-_LEDGER_KEEP:]}, indent=1))


# --- audit log: a permanent, appendable record for a takedown or a rights --
# inquiry to be answered from, in Excel-openable CSV rather than something
# that needs this codebase to read.

def log_credit(row: dict) -> None:
    CREDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    is_new = not CREDIT_LOG.exists()
    with CREDIT_LOG.open("a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_CREDIT_LOG_FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in _CREDIT_LOG_FIELDS})
