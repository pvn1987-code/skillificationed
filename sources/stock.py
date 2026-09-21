"""Pexels video, behind a hard verification gate.

The gate is the entire point. Pexels never returns an empty result set: asked
for "Elon Musk" it reports 1,326 results and offers a drone shot of a beach at
sunset, a satellite dish, and an unrelated car in a showroom. A pipeline that
takes the top hit will therefore caption a stranger's holiday footage with a
billionaire's name and never notice.

So a clip is accepted only when the library's own description of it -- the
human-written page slug -- contains the subject's distinctive words. "Niagara
Falls" must land on a page that says niagara, not on any waterfall. If nothing
does, this module returns None, and the caller moves down the ladder rather
than settling for a near-miss.

Licence: the Pexels licence permits free commercial use without attribution.
The creator is recorded anyway so a credit line always exists.
"""
from pathlib import Path

import requests

import config

SEARCH = "https://api.pexels.com/videos/search"
_TIMEOUT = 30
_GOOD_DURATION = 8          # preferred, so a 20s clip beats a 10s one
_SEARCH_CACHE = {}


class StockError(RuntimeError):
    pass


class StockQuotaError(StockError):
    """Rate limited (200/hour). The caller waits rather than failing."""


def _slug(video: dict) -> str:
    return (video.get("url") or "").rstrip("/").rsplit("/", 1)[-1].lower()


def _slug_tokens(video: dict) -> set:
    """Words of the page slug, minus the trailing numeric id."""
    return {w for w in _slug(video).split("-") if not w.isdigit()}


def match_ratio(video: dict, distinctive: set) -> float:
    """Share of the subject's distinctive words present in the page slug.

    Stemmed loosely so plurals and spelling variants still count ("centre" /
    "center"), but never so loosely that a different subject can satisfy it.
    """
    if not distinctive:
        return 0.0
    words = _slug_tokens(video)
    hits = 0
    for term in distinctive:
        if term in words:
            hits += 1
        elif len(term) >= 5 and any(term[:5] in w for w in words):
            hits += 1
    return hits / len(distinctive)


def _pick_file(video: dict) -> dict | None:
    """Smallest portrait mp4 that still clears the minimum width.

    video_files mixes sd/hd/uhd in no particular order, so the choice has to be
    explicit rather than "take the first".
    """
    candidates = [
        f for f in video.get("video_files") or []
        if f.get("file_type") == "video/mp4"
        and (f.get("width") or 0) >= config.MIN_CLIP_WIDTH
        and (f.get("height") or 0) >= (f.get("width") or 0)
    ]
    return min(candidates, key=lambda f: f["width"]) if candidates else None


def search(keyword: str, per_page: int = 40) -> list:
    """One API call per keyword per run -- the hourly limit is only 200."""
    if keyword in _SEARCH_CACHE:
        return _SEARCH_CACHE[keyword]
    if not config.PEXELS_API_KEY:
        raise StockError("PEXELS_API_KEY is not set in .env")
    response = requests.get(
        SEARCH, headers={"Authorization": config.PEXELS_API_KEY},
        params={"query": keyword, "orientation": "portrait",
                "size": "medium", "per_page": per_page}, timeout=_TIMEOUT)
    if response.status_code == 429:
        raise StockQuotaError("Pexels rate limit reached (200/hour)")
    if response.status_code == 401:
        raise StockError("Pexels rejected the API key")
    if response.status_code != 200:
        raise StockError(f"Pexels {response.status_code}: {response.text[:200]}")
    videos = response.json().get("videos") or []
    _SEARCH_CACHE[keyword] = videos
    return videos


def _download(url: str, dest: Path) -> None:
    """Streamed via a .part file, so an interrupted run cannot leave a
    half-written clip in the cache to be trusted on the next one."""
    part = dest.with_suffix(".part")
    with requests.get(url, stream=True, timeout=_TIMEOUT) as response:
        response.raise_for_status()
        with part.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1 << 16):
                handle.write(chunk)
    part.replace(dest)


PHOTO_SEARCH = "https://api.pexels.com/v1/search"
_PHOTO_CACHE = {}


def search_photos(query: str, per_page: int = 40) -> list:
    """Portrait stock STILLS. Same key and account as the clip search.

    A photo dict carries the same `url` slug a video does, so match_ratio and
    the slug screens apply to it unchanged.
    """
    if query in _PHOTO_CACHE:
        return _PHOTO_CACHE[query]
    if not config.PEXELS_API_KEY:
        raise StockError("PEXELS_API_KEY is not set in .env")
    response = requests.get(
        PHOTO_SEARCH, headers={"Authorization": config.PEXELS_API_KEY},
        params={"query": query, "orientation": "portrait",
                "size": "large", "per_page": per_page}, timeout=_TIMEOUT)
    if response.status_code == 429:
        raise StockQuotaError("Pexels rate limit reached (200/hour)")
    if response.status_code != 200:
        raise StockError(f"Pexels {response.status_code}: {response.text[:120]}")
    photos = response.json().get("photos") or []
    _PHOTO_CACHE[query] = photos
    return photos


def find_photo(query: str, distinctive, seen: set | None = None,
               min_ratio: float | None = None) -> tuple | None:
    """Best on-query portrait still, gated by the same ratio clips are."""
    floor = config.ILLUSTRATIVE_MATCH_RATIO if min_ratio is None else min_ratio
    seen = seen if seen is not None else set()
    ranked = []
    for photo in search_photos(query):
        if photo.get("id") in seen:
            continue
        ratio = match_ratio(photo, distinctive)
        if ratio < floor:
            continue
        ranked.append((ratio, photo))
    if not ranked:
        return None
    ranked.sort(key=lambda pair: -pair[0])
    ratio, photo = ranked[0]
    src = photo.get("src") or {}
    url = src.get("portrait") or src.get("large2x") or src.get("original")
    if not url:
        return None
    dest = config.ASSET_CACHE / f"pexels-photo-{photo['id']}.jpg"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not (dest.exists() and dest.stat().st_size > 0):
        _download(url, dest)
    seen.add(photo["id"])
    return dest, {
        "asset_id": f"pexels-photo-{photo['id']}",
        "page": photo.get("url", ""),
        "author": photo.get("photographer", ""),
        "author_url": photo.get("photographer_url", ""),
        "source": "pexels-photo",
        "match_ratio": round(ratio, 2),
    }


def fetch_by_id(video_id: int, seen: set | None = None) -> tuple | None:
    """One specific clip, by Pexels id. No search, no slug gate.

    For the cases the slug language cannot express. "Car jack lifting vehicle"
    and "luxury sports car on vehicle lift" share the words a ratio can see,
    so a two-post workshop lift scores exactly as well as a jack under a wheel
    -- and a tutorial step that says "jack" cannot show a lift. When a person
    has watched a clip and knows it is right, the id IS the evidence and the
    gate has nothing left to add.

    Bypassing the gate is the whole point, so it is only ever reached from an
    explicit `pexels_ids` in a spec, never from a search result.
    """
    if not config.PEXELS_API_KEY:
        raise StockError("PEXELS_API_KEY is not set in .env")
    if seen is not None and video_id in seen:
        return None
    response = requests.get(f"https://api.pexels.com/videos/videos/{video_id}",
                            headers={"Authorization": config.PEXELS_API_KEY},
                            timeout=_TIMEOUT)
    if response.status_code == 429:
        raise StockQuotaError("Pexels rate limit reached")
    if response.status_code != 200:
        raise StockError(f"Pexels {response.status_code} for clip {video_id}")
    video = response.json()
    chosen = _pick_file(video)
    if not chosen:
        raise StockError(f"clip {video_id} has no usable portrait rendition")
    dest = config.ASSET_CACHE / f"pexels-{video_id}-{chosen['id']}.mp4"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not (dest.exists() and dest.stat().st_size > 0):
        _download(chosen["link"], dest)
    if seen is not None:
        seen.add(video_id)
    return dest, {
        "asset_id": f"pexels-{video_id}",
        "page": video.get("url", ""),
        "author": (video.get("user") or {}).get("name", ""),
        "author_url": (video.get("user") or {}).get("url", ""),
        "source": "pexels",
        "match_ratio": "pinned",
        "duration": video.get("duration"),
    }


def probe(terms: list, distinctive, seen: set | None = None,
          min_ratio: float | None = None) -> tuple:
    """(best ratio, how many clips would pass) WITHOUT downloading anything.

    The pre-flight needs to know whether an item can be carried before the
    build spends minutes on speech and Ken Burns renders. Search results are
    cached in-process, so a later find() for the same terms costs no extra
    call.
    """
    if isinstance(distinctive, (set, frozenset)):
        distinctive = [distinctive]
    alternatives = [d for d in (distinctive or []) if d]
    required = config.SLUG_MATCH_RATIO if min_ratio is None else min_ratio
    seen = seen or set()
    best, passing = 0.0, 0
    for term in terms:
        try:
            results = search(term)
        except StockQuotaError:
            raise
        except StockError:
            continue
        for video in results:
            if video["id"] in seen:
                continue
            if (video.get("duration") or 0) < config.MIN_CLIP_SECONDS:
                continue
            if not _pick_file(video):
                continue
            ratio = (max(match_ratio(video, alt) for alt in alternatives)
                     if alternatives else 1.0)
            best = max(best, ratio)
            if ratio >= required:
                passing += 1
    return best, passing


def find(terms: list, distinctive, seen: set | None = None,
         min_ratio: float | None = None, screen=None) -> tuple | None:
    """Best VERIFIED clip across several phrasings of one subject.

    `distinctive` is either one set of tokens or a LIST of alternative token
    sets (see Entity.identifiers) -- a clip passes by satisfying any one of
    them, so "NYC" and "New York City" both count while neither can make the
    other stricter.

    An empty set means the caller wants mood footage and has nothing to verify
    against, so the gate is skipped deliberately. That is why the ladder never
    passes an item's name in with an empty set.
    """
    if isinstance(distinctive, (set, frozenset)):
        distinctive = [distinctive]
    alternatives = [d for d in (distinctive or []) if d]
    seen = seen if seen is not None else set()
    required = config.SLUG_MATCH_RATIO if min_ratio is None else min_ratio
    cache = config.ASSET_CACHE
    cache.mkdir(parents=True, exist_ok=True)

    scored = []
    for term in terms:
        try:
            results = search(term)
        except StockQuotaError:
            raise
        except StockError:
            continue
        for video in results:
            if video["id"] in seen:
                continue
            if (video.get("duration") or 0) < config.MIN_CLIP_SECONDS:
                continue
            if not _pick_file(video):
                continue
            ratio = (max(match_ratio(video, alt) for alt in alternatives)
                     if alternatives else 1.0)
            if ratio < required:
                continue
            scored.append((ratio, video, term))

    if not scored:
        return None
    # Best match first; among equals prefer a comfortably long clip, since a
    # short one has to loop to fill a shot and the loop is visible.
    scored.sort(key=lambda row: (-row[0],
                                 -min(row[1].get("duration") or 0, _GOOD_DURATION),
                                 -(row[1].get("duration") or 0)))

    for ratio, video, term in scored:
        chosen = _pick_file(video)
        dest = cache / f"pexels-{video['id']}-{chosen['id']}.mp4"
        if not (dest.exists() and dest.stat().st_size > 0):
            try:
                _download(chosen["link"], dest)
            except requests.RequestException:
                continue
        # Face screening needs the bytes, so it can only run post-download. A
        # rejected clip stays cached: re-fetching it to reach the same verdict
        # would be the only thing worse.
        if screen is not None and screen(dest):
            seen.add(video["id"])
            continue
        seen.add(video["id"])
        credit = {
            "source": "pexels",
            "query": term,
            "match_ratio": round(ratio, 2),
            "asset_id": video["id"],
            "page": video.get("url", ""),
            "author": (video.get("user") or {}).get("name", ""),
            "author_url": (video.get("user") or {}).get("url", ""),
            "licence": "Pexels Licence",
            "attribution_required": False,
            "resolution": f"{chosen['width']}x{chosen['height']}",
            "duration": video.get("duration"),
            "kind": "video",
        }
        return dest, credit
    return None
