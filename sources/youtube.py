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
import json
import subprocess
import sys
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
