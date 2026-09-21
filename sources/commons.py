"""Wikimedia Commons: media that Wikidata itself attaches to the subject.

Where a stock library is guessing from a keyword, this is authoritative. An
entity's P18 is the image Wikipedia shows for that entity -- it IS the subject,
settled by editors, not by a ranking function. That is what makes footage of a
named person possible at all: no stock library licenses celebrity footage, so
without this route "Elon Musk" can only ever be something vaguely electric.

Two cautions learned from reading real category listings:

1. A Commons category holds far more than photographs of its subject. The
   Elon Musk category returns a signature, a net-worth chart, source code, a
   PDF, and -- the one that would be genuinely damaging -- AI-GENERATED
   DEEPFAKE videos. Titles matching config.COMMONS_DENY never enter a reel.

2. PNG on Commons is overwhelmingly diagrams, logos and screenshots, while
   photographs are JPEG. So JPEGs are strongly preferred and a PNG is taken
   only when nothing else exists.

Licences vary (public domain, CC BY, CC BY-SA). Attribution is captured for
every file and the non-public-domain ones are credited on a closing card.
"""
import html
import json
import re
import time
from pathlib import Path

import requests

import config

_API = "https://commons.wikimedia.org/w/api.php"
_TIMEOUT = 30
# Banner- and column-shaped files are screenshots and charts, not photographs.
_MIN_ASPECT, _MAX_ASPECT = 0.4, 2.2
# Output is 1080 wide; kenburns supersamples internally to avoid zoompan
# jitter, so the SOURCE does not need to be huge. 1600px is indistinguishable
# at 1080 and roughly a tenth the bytes.
_THUMB_WIDTH = 1600
_IMAGE_MIME = ("image/jpeg", "image/png")
_VIDEO_MIME = ("video/webm", "video/ogg", "application/ogg", "video/mp4")

# File metadata is stable, so it is cached on disk. This is not only a speed
# win: the footage pre-flight asks Commons about every item, and the build then
# asked again, which doubled the call count and tripped throttling -- the audit
# would report an item fine and the build would then find nothing for it. With
# the cache, the build reuses exactly what the audit saw, so the two cannot
# disagree.
_MEDIA_CACHE_FILE = config.STATE_DIR / "commons_media.json"
_MEDIA_CACHE_VERSION = 2
_media_cache = None

_session = None
_last_call = [0.0]
_MIN_INTERVAL = [0.35]     # see entities._MIN_INTERVAL: Commons throttles too


def _http():
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({"User-Agent": config.WIKI_USER_AGENT})
    return _session


class CommonsError(RuntimeError):
    pass


def _get(params: dict, attempts: int = 3) -> dict:
    last = None
    for attempt in range(attempts):
        try:
            gap = time.monotonic() - _last_call[0]
            if gap < _MIN_INTERVAL[0]:
                time.sleep(_MIN_INTERVAL[0] - gap)
            _last_call[0] = time.monotonic()
            response = _http().get(_API, params={**params, "format": "json"},
                                   timeout=_TIMEOUT)
            if response.status_code in (429, 503):
                # Back off for the REST of the run, not just this call. A
                # throttle means the current pace is too fast, and the run has
                # many more lookups to make; a per-call sleep alone let the
                # next item hit the same wall and resolve to nothing.
                _MIN_INTERVAL[0] = min(_MIN_INTERVAL[0] * 2, 3.0)
                time.sleep(min(float(response.headers.get("Retry-After") or
                                     (attempt + 1) * 2), 10))
                last = RuntimeError(f"commons {response.status_code}")
                continue
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            last = exc
            time.sleep((attempt + 1) * 1.5)
    raise CommonsError(f"commons unreachable: {last}")


def _strip_html(value: str) -> str:
    """Commons returns artist names as HTML anchors; a credits card needs text."""
    return html.unescape(re.sub(r"<[^>]+>", "", value or "")).strip()


def _denied(title: str) -> str:
    """The deny term this title trips, or '' when it is clean."""
    lowered = title.lower()
    for term in config.COMMONS_DENY:
        if term in lowered:
            return term
    return ""


def file_info(titles: list) -> list:
    """Metadata for up to 50 File: titles in one call."""
    if not titles:
        return []
    data = _get({"action": "query", "titles": "|".join(titles[:50]),
                 "prop": "imageinfo",
                 "iiprop": "url|size|mime|extmetadata",
                 # Wikimedia's own 429 says to use thumbnails rather than
                 # originals, and it is right: a 12288px panorama was being
                 # fetched to render a 1080px-wide video. This asks the API for
                 # a rendition wide enough for the Ken Burns source and no more.
                 "iiurlwidth": _THUMB_WIDTH})
    out = []
    for page in (data.get("query", {}).get("pages", {}) or {}).values():
        info = (page.get("imageinfo") or [{}])[0]
        if not info.get("url"):
            continue
        meta = info.get("extmetadata", {}) or {}
        out.append({
            "title": page.get("title", ""),
            "url": info.get("url", ""),
            "thumb": info.get("thumburl", ""),
            "mime": info.get("mime", ""),
            "width": info.get("width") or 0,
            "height": info.get("height") or 0,
            "licence": _strip_html(meta.get("LicenseShortName", {}).get("value", "")),
            "author": _strip_html(meta.get("Artist", {}).get("value", "")),
            "page": info.get("descriptionurl", ""),
        })
    return out


def _usable(info: dict, distinctive: set, want_video: bool,
            pinned: bool = False) -> bool:
    """Whether one Commons file may stand for the subject.

    `pinned` marks the file Wikidata itself attaches to the entity (P18/P10).
    That file is the subject by editorial decision, so it is exempt from the
    heuristics below -- all of which exist to filter a CATEGORY, which is a far
    looser association. Two real failures from the first version:

      - Taylor Swift's P18 is a PNG, so the "photographs are JPEG" preference
        discarded it and promoted a category file of the Taylor Swift
        Education CENTER: a building named after her, not her.
      - Niagara Falls' P18 is a wide panorama, so the aspect check dropped it
        and the top pick became a REPLICA of the falls in a shop window.

    Both are exactly the substitution this project exists to prevent, and in
    both the authoritative answer was sitting right there.
    """
    if _denied(info["title"]):
        return False
    mime = info["mime"]
    if want_video:
        if mime not in _VIDEO_MIME:
            return False
    elif mime not in _IMAGE_MIME:
        return False
    if not want_video:
        if not pinned and info["width"] < config.COMMONS_MIN_WIDTH:
            return False
        aspect = info["width"] / max(info["height"], 1)
        # A pinned file is allowed to be panoramic -- it gets a centre crop --
        # but past about 3:1 a 9:16 crop keeps too little of the frame.
        low, high = (_MIN_ASPECT, _MAX_ASPECT) if not pinned else (0.28, 3.0)
        if not low <= aspect <= high:
            return False
    if pinned:
        return True
    # Category membership can be loose ("Community Notes under Elon Musk free
    # speech tweet"), so the file's own title must still name the subject.
    if distinctive:
        lowered = info["title"].lower()
        if not any(term in lowered for term in distinctive):
            return False
    return True


def category_files(category: str, limit: int = 60) -> list:
    """File titles in a Commons category, newest-id first is not offered, so
    this is the alphabetical page the API gives and the caller filters it."""
    data = _get({"action": "query", "list": "categorymembers",
                 "cmtitle": f"Category:{category}", "cmtype": "file",
                 "cmlimit": min(limit, 500)})
    return [m["title"] for m in
            (data.get("query", {}).get("categorymembers", []) or [])]


def _load_media_cache() -> dict:
    global _media_cache
    if _media_cache is None:
        try:
            _media_cache = json.loads(_MEDIA_CACHE_FILE.read_text())
        except (OSError, ValueError):
            _media_cache = {}
    return _media_cache


def _save_media_cache() -> None:
    try:
        _MEDIA_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _MEDIA_CACHE_FILE.write_text(json.dumps(_media_cache, indent=1))
    except (OSError, TypeError):
        pass


def entity_media(entity, want_video: bool = False, limit: int = None) -> list:
    """Usable Commons files for a resolved entity, most authoritative first.

    Order matters: P18/P10 are what Wikidata's editors chose to represent the
    subject, so they lead. Category files follow, filtered hard.
    """
    limit = limit or config.COMMONS_MAX_FILES
    if not entity.resolved:
        return []
    cache = _load_media_cache()
    key = f"v{_MEDIA_CACHE_VERSION}|{entity.qid}|{int(want_video)}|{limit}"
    if key in cache:
        return cache[key]
    distinctive = entity.distinctive()

    titles = []
    pinned = entity.video if want_video else entity.image
    if pinned:
        titles.append(f"File:{pinned}")
    if entity.commons_cat:
        try:
            titles += [t for t in category_files(entity.commons_cat)
                       if t not in titles]
        except CommonsError:
            pass

    try:
        infos = file_info(titles)
    except CommonsError:
        return []
    by_title = {i["title"]: i for i in infos}
    # file_info does not preserve the requested order, so re-impose it: the
    # pinned P18 must stay first or the authoritative pick loses to whatever
    # the API happened to return first.
    ordered = [by_title[t] for t in titles if t in by_title]

    pinned_title = f"File:{pinned}" if pinned else ""
    head = [i for i in ordered
            if i["title"] == pinned_title
            and _usable(i, distinctive, want_video, pinned=True)]
    rest = [i for i in ordered
            if i["title"] != pinned_title
            and _usable(i, distinctive, want_video)]
    if not want_video:
        # Photographs are JPEG on Commons; PNG is diagrams, logos and
        # screenshots. For a CATEGORY file that rule is absolute rather than a
        # preference: falling back to PNG when no JPEG existed offered three
        # Grokipedia screenshots and a German net-worth chart as footage of
        # Elon Musk. The pinned P18 is exempt -- Taylor Swift's is a PNG.
        rest = [i for i in rest if i["mime"] == "image/jpeg"]
    for info in head:
        info["pinned"] = True
    chosen = (head + rest)[:limit]
    # Only a successful lookup is cached. An empty result may mean the API was
    # throttled, and caching that would make a momentary blip permanent.
    if chosen:
        cache[key] = chosen
        _save_media_cache()
    return chosen


def download(info: dict, attempts: int = 4) -> Path:
    """Fetch one Commons file into the asset cache, streamed via .part.

    Goes through the same rate limiter as the API calls. It did not, and that
    was the bug: `upload.wikimedia.org` started returning 429 mid-build, every
    photograph failed, and four counties the pre-flight had just confirmed were
    fine ended up on mood footage. A video file still uses the original -- there
    is no thumbnail of a video.
    """
    cache = config.ASSET_CACHE
    cache.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9._-]", "_", info["title"].replace("File:", ""))
    dest = cache / f"commons-{name}"[:180]
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    source = info.get("thumb") or info["url"]
    part = dest.with_suffix(dest.suffix + ".part")
    last = None
    for attempt in range(attempts):
        try:
            gap = time.monotonic() - _last_call[0]
            if gap < _MIN_INTERVAL[0]:
                time.sleep(_MIN_INTERVAL[0] - gap)
            _last_call[0] = time.monotonic()
            with _http().get(source, stream=True, timeout=_TIMEOUT) as response:
                if response.status_code in (429, 503):
                    _MIN_INTERVAL[0] = min(_MIN_INTERVAL[0] * 2, 3.0)
                    time.sleep(min(float(response.headers.get("Retry-After")
                                         or (attempt + 1) * 3), 15))
                    last = RuntimeError(f"upload {response.status_code}")
                    continue
                response.raise_for_status()
                with part.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1 << 16):
                        handle.write(chunk)
            part.replace(dest)
            return dest
        except requests.RequestException as exc:
            last = exc
            time.sleep((attempt + 1) * 2)
    raise CommonsError(f"could not fetch {info['title']}: {last}")


def credit_for(info: dict, query: str = "") -> dict:
    licence = info.get("licence", "")
    return {
        "source": "wikimedia commons",
        "query": query,
        "match_ratio": 1.0,          # attached to the entity, not keyword-matched
        "asset_id": info.get("title", ""),
        "page": info.get("page", ""),
        "author": info.get("author", ""),
        "author_url": "",
        "licence": licence or "unknown",
        # Public-domain files need no credit; everything else does, and the
        # closing card is built from exactly this flag.
        "attribution_required": "public domain" not in licence.lower()
                                and "pd" != licence.lower().strip(),
        "resolution": f"{info.get('width')}x{info.get('height')}",
        "duration": None,
        "kind": "video" if info.get("mime", "").startswith("video") else "image",
    }
