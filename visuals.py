"""The authenticity ladder: one list item in, real footage of it out.

Every rung is verified against something outside a search ranking, and every
result carries the tier it was found at, so the manifest can always answer
"why is this shot here?".

    EXACT    the subject itself.
             - a stock clip whose own page slug names it, or
             - a photograph Wikidata attaches to the entity, given motion.
    PROXY    a real, related entity when the subject has no usable footage:
             Elon Musk -> Tesla, SpaceX. Still true about the item; it is
             never presented as a picture of the person.
    GENERIC  matched nothing. Mood footage. Reserved for beats that have no
             subject -- the hook, the mid-roll tease, the close -- and never
             allowed to carry a numbered item.

Why stills rather than video for people: no stock library licenses footage of
named individuals, and Commons video of a public figure is usually a full
speech whose opening seconds are a wide shot of a lectern. The photograph
Wikidata itself picked is a guaranteed likeness, and kenburns makes it move.
"""
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import config
import entities
import kenburns
import screening
from sources import commons, stock, youtube


@dataclass
class Shot:
    """One ready-to-cut portrait clip, with its provenance."""
    path: Path
    credit: dict
    tier: str
    subject: str                  # what is actually on screen
    note: str = ""


@dataclass
class ItemVisuals:
    name: str
    entity: object = None
    shots: list = field(default_factory=list)
    tier: str = config.TIER_GENERIC

    @property
    def authentic(self) -> bool:
        return self.tier == config.TIER_EXACT


# Words that describe framing rather than subject; they are rarely in a slug
# and would drag the match ratio down for no reason.
_QUERY_NOISE = {"aerial", "drone", "shot", "view", "footage", "video", "4k",
                "sunny", "beautiful", "stunning", "or", "and", "the", "a", "of"}


# Landscape and building words that describe a GENERIC scene. Any clip on
# earth can satisfy one of these, so they may never carry a relaxed match on
# their own -- "valley" is what let a Turkish hillside stand in for Silicon
# Valley.
_QUERY_SCENERY = {"valley", "mountain", "mountains", "hill", "hills", "beach",
                  "coast", "lake", "river", "forest", "desert", "sky", "cloud",
                  "clouds", "sunset", "sunrise", "city", "town", "street",
                  "road", "park", "house", "home", "building", "buildings",
                  "office", "landscape", "neighborhood", "neighbourhood",
                  "suburban", "suburb", "water", "ocean", "sea", "field",
                  "garden", "tree", "trees", "night", "day"}


def _query_tokens(phrase: str, distinctive_only: bool = False) -> set:
    """The content words of an author's query, for matching against a slug.

    `distinctive_only` drops generic scenery words, leaving just the ones that
    actually pin the shot down. Used for the relaxed round, where a single word
    is enough to accept a clip and so that word had better mean something.
    """
    words = {w for w in re.split(r"\W+", phrase.lower()) if len(w) > 2}
    content = (words - _QUERY_NOISE) or words
    if not distinctive_only:
        return content
    return content - _QUERY_SCENERY


def _screen():
    return screening.has_faces if config.REJECT_FACES else None


def _stock_shots(entity, wanted: int, seen: set, log, query: str = "") -> list:
    """Verified stock video of this entity, or nothing. Never a near-miss.

    `query` is a caller-supplied search phrase (a spec's `pexels_query`). It
    changes only WHAT IS SEARCHED, never what is accepted: the result still has
    to name the entity, so a hand-written query cannot smuggle in footage of
    something else.
    """
    terms = ([query] + entity.search_terms()) if query else entity.search_terms()
    shots = []
    for _ in range(wanted):
        found = stock.find(terms, entity.identifiers(),
                           seen=seen, screen=_screen())
        if not found:
            break
        path, credit = found
        shots.append(Shot(path=path, credit=credit, tier=config.TIER_EXACT,
                          subject=entity.label or entity.name,
                          note=f"slug match {credit['match_ratio']}"))
        log(f"      stock: {credit['page'].rsplit('/', 2)[-2] if credit['page'] else credit['asset_id']}"
            f"  match={credit['match_ratio']}")
    return shots


def _commons_shots(entity, wanted: int, seen: set, log, day_dir: Path) -> list:
    """Wikidata's own photographs of this entity, given motion.

    Two shots from ONE photograph are allowed, pushing in and then pulling out,
    because a second authentic image often does not exist and a second angle on
    the real subject beats a first angle on the wrong one.
    """
    try:
        infos = commons.entity_media(entity)
    except Exception as exc:  # noqa: BLE001 -- a source failing is not fatal
        log(f"      commons unavailable: {exc}")
        infos = []
    # Wikidata says this entity HAS a picture, so an empty result is a failed
    # fetch, not an absent photograph. Worth one slow retry on an unattended
    # run: an audit lost Larry Page to exactly this while every other lookup
    # in the same pass succeeded.
    if not infos and entity.image:
        log("      commons returned nothing for an entity with a P18 — retrying")
        time.sleep(5)
        try:
            infos = commons.entity_media(entity)
        except Exception:  # noqa: BLE001
            infos = []
    if not infos:
        return []

    shots = []
    directions = ("in", "out", "in", "out")
    anchor = "top" if entity.kind == "person" else "centre"
    for index in range(wanted):
        info = infos[index] if index < len(infos) else infos[0]
        key = f"{info['title']}|{directions[index % 4]}"
        if key in seen:
            continue
        try:
            source = commons.download(info)
        except Exception as exc:  # noqa: BLE001
            log(f"      commons download failed: {exc}")
            continue
        clip = day_dir / ("kb-" + f"{abs(hash(key)) & 0xFFFFFFF:x}.mp4")
        if not clip.exists():
            try:
                kenburns.render(source, clip, direction=directions[index % 4],
                                anchor=anchor)
            except kenburns.KenBurnsError as exc:
                log(f"      ken burns failed: {exc}")
                continue
        seen.add(key)
        credit = commons.credit_for(info, query=entity.label or entity.name)
        credit["motion"] = f"ken burns {directions[index % 4]}"
        shots.append(Shot(path=clip, credit=credit, tier=config.TIER_EXACT,
                          subject=entity.label or entity.name,
                          note="wikidata-attached" if info.get("pinned")
                               else "commons category"))
        log(f"      commons: {info['title'][:48]} ({info['licence']})"
            + ("  [P18]" if info.get("pinned") else ""))
    return shots


def _photo_shots(query: str, name: str, wanted: int, seen: set, log,
                 day_dir: Path) -> list:
    """Pexels STILLS for an authored query, given motion by Ken Burns.

    Stock libraries hold far more photographs than clips, and a photograph is
    shot to be legible in one frame where a clip is shot to move -- so for an
    instruction like "loosen the nuts before you jack", a still is often the
    more literal picture of the step. The cost is that it does not move on its
    own, which is what the Ken Burns push is for.

    Same slug gate as the clip path: a photograph that does not carry the
    author's words is no more use than a clip that does not.
    """
    distinctive = _query_tokens(query, distinctive_only=True) or _query_tokens(query)
    shots, directions = [], ("in", "out", "in", "out")
    for index in range(wanted):
        try:
            found = stock.find_photo(query, distinctive, seen=seen)
        except Exception as exc:                  # noqa: BLE001
            log(f"      photo search failed: {exc}")
            break
        if not found:
            break
        source, credit = found
        direction = directions[index % 4]
        clip = day_dir / f"kbp-{source.stem}-{direction}.mp4"
        if not clip.exists():
            try:
                kenburns.render(source, clip, direction=direction)
            except kenburns.KenBurnsError as exc:
                log(f"      ken burns failed: {exc}")
                continue
        credit["motion"] = f"ken burns {direction}"
        shots.append(Shot(path=clip, credit=credit,
                          tier=config.TIER_ILLUSTRATIVE, subject=query,
                          note=f"photo, slug match {credit['match_ratio']}"))
        log(f"      photo: {credit['page'].rstrip('/').rsplit('/', 1)[-1][:44]}"
            f"  match={credit['match_ratio']}")
    return shots


def pinned_shots(ids: list, name: str, wanted: int, seen: set, log) -> list:
    """Clips a person chose by id, in the order they chose them.

    For the cases the slug language cannot express. "car jack lifting vehicle"
    and "luxury sports car on vehicle lift" share every word a ratio can see,
    so a two-post workshop lift scores exactly as well as a jack under a wheel
    -- and a tutorial step that says "jack" must not show a lift. When someone
    has watched the frames, the id IS the evidence.
    """
    shots = []
    for video_id in ids:
        try:
            found = stock.fetch_by_id(int(video_id), seen=seen)
        except Exception as exc:                  # noqa: BLE001
            log(f"      ! pinned clip {video_id}: {exc}")
            continue
        if not found:
            continue
        path, credit = found
        shots.append(Shot(path=path, credit=credit,
                          tier=config.TIER_ILLUSTRATIVE,
                          subject=name, note="pinned by id"))
        log(f"      pinned: {video_id}")
        if len(shots) >= wanted:
            break
    return shots


def youtube_shots(entries: list, name: str, wanted: int, log) -> list:
    """A person's own YouTube pick, same accountability as pinned_shots.

    Real footage of a specific named subject (a particular temple's idol, a
    particular festival) essentially does not exist as licensed stock -- but
    it exists on YouTube constantly, almost always under YouTube's default
    Standard license, which permits none of this. Each entry here is
    `{"id": ..., "start": seconds, "seconds": length}` (start/seconds
    optional); sources.youtube.fetch_by_id refuses anything that is not
    actually, verifiably Creative Commons, whatever the title claims.
    """
    shots = []
    for entry in entries:
        if isinstance(entry, str):
            entry = {"id": entry}
        video_id = entry.get("id", "")
        try:
            path, credit = youtube.fetch_by_id(
                video_id, start_seconds=float(entry.get("start", 0.0)),
                clip_seconds=float(entry.get("seconds", 6.0)))
        except youtube.YouTubeError as exc:
            log(f"      ! youtube {video_id}: {exc}")
            continue
        shots.append(Shot(path=path, credit=credit,
                          tier=config.TIER_ILLUSTRATIVE,
                          subject=name, note="pinned from youtube (CC-verified)"))
        log(f"      youtube: {video_id} ({credit.get('author', '')})")
        if len(shots) >= wanted:
            break
    return shots


def _authored_shots(query: str, name: str, wanted: int, seen: set, log) -> list:
    """The shots a person asked for, gated on their own words.

    "A or B" is how someone writes alternatives, not a search string, so the
    query is split on it and each phrase tried in turn.
    """
    phrases = [part.strip() for part in re.split(r"\s+or\s+", query)
               if part.strip()]
    shots = []
    # Two passes: one shot from each phrase first, so alternatives get used,
    # then a second round to fill the item's remaining shots.
    # The relaxed round exists because a descriptive phrase does not always
    # share words with a slug: "Silicon valley tech office aerial drone" found
    # nothing at the normal threshold and the item fell back to a photograph of
    # the county government centre -- strictly correct, and the opposite of
    # what was asked for. Half the threshold still requires the clip to carry
    # some of the author's own words, so it cannot drift into a named place.
    # The relaxed round drops the threshold but NARROWS the words that may
    # satisfy it, which is the opposite of how it was first written. Halving
    # the ratio alone let a single common word carry a match: "Silicon valley
    # tech office aerial drone" matched on `valley` and put a hillside in
    # Turkey -- flag visible in frame -- under "SANTA CLARA COUNTY, CA". That
    # is precisely the substitution a viewer would catch. In the relaxed round
    # only the query's distinctive words count, so `silicon`, `tech` or
    # `office` can carry it and `valley` cannot.
    rounds = [(config.ILLUSTRATIVE_MATCH_RATIO, False),
              (config.ILLUSTRATIVE_MATCH_RATIO, False),
              (config.ILLUSTRATIVE_RELAXED_RATIO, True)]
    for ratio, strict_words in rounds:
        for phrase in phrases:
            if len(shots) >= wanted:
                break
            tokens = _query_tokens(phrase, distinctive_only=strict_words)
            if not tokens:
                continue
            found = stock.find([phrase], [tokens], seen=seen,
                               min_ratio=ratio, screen=_screen())
            if not found:
                continue
            path, credit = found
            shots.append(Shot(path=path, credit=credit,
                              tier=config.TIER_ILLUSTRATIVE, subject=phrase,
                              note=f"your shot for {name}"))
            log(f"      yours: '{phrase[:38]}' -> {Path(path).name[:34]}")
    return shots


def for_item(name: str, kind: str, seen: set, log, day_dir: Path,
             wanted: int | None = None, query: str = "",
             depictable: bool = False,
             pinned: list | None = None,
             stills: bool = False,
             youtube_ids: list | None = None) -> ItemVisuals:
    """Walk the ladder for one numbered item."""
    wanted = wanted or config.SHOTS_PER_ITEM

    # An author-supplied query is an INSTRUCTION, so it runs before anything
    # else -- it does not even need the entity resolved.
    #
    # The first attempt put this after the entity lookup, so Commons returned
    # the county courthouse and that won: a spec asking for "Silicon valley
    # tech office aerial drone" still got a photograph of the Santa Clara
    # government centre. Nobody can identify a county from a photograph, so the
    # strictly-correct shot was also the useless one.
    #
    # The gate does not disappear here, it changes question: instead of "is
    # this the subject?" it asks "is this what was asked for?", matching the
    # clip against the QUERY's own words. That is what stops a famous landmark
    # wandering in under someone else's name -- the one substitution a viewer
    # really would catch.
    # `depictable` is the guardrail on all of this. When a viewer WOULD
    # recognise the subject in a photograph -- Niagara Falls, Elon Musk --
    # footage of the real thing must win, and the query is demoted to a search
    # hint. Letting a model-written query outrank verified footage there would
    # reopen the exact bug this project was built to prevent: a generic
    # waterfall captioned NIAGARA FALLS.
    #
    # It is only when nobody could recognise the subject that the query is
    # allowed to carry the item.
    # A pinned id outranks everything else: it is the only input carrying a
    # person's eyes on the actual frames rather than on a slug.
    if pinned:
        found = pinned_shots(pinned, name, wanted, seen, log)
        if found:
            result = ItemVisuals(name=name, shots=found[:wanted],
                                 tier=config.TIER_ILLUSTRATIVE)
            while len(result.shots) < wanted:
                result.shots.append(result.shots[0])
            return result
        log(f"      ! no pinned clip resolved for {name}; falling back")

    # Same trust tier as a Pexels pin -- a person watched the actual frames --
    # for the far more common case that no stock library has the specific
    # named subject at all. sources.youtube refuses anything not verifiably
    # Creative Commons, so this can never silently become unlicensed footage.
    if youtube_ids:
        found = youtube_shots(youtube_ids, name, wanted, log)
        if found:
            result = ItemVisuals(name=name, shots=found[:wanted],
                                 tier=config.TIER_ILLUSTRATIVE)
            while len(result.shots) < wanted:
                result.shots.append(result.shots[0])
            return result
        log(f"      ! no youtube clip resolved for {name}; falling back")

    if query and stills:
        found = _photo_shots(query, name, wanted, seen, log, day_dir)
        if found:
            result = ItemVisuals(name=name, shots=found[:wanted],
                                 tier=config.TIER_ILLUSTRATIVE)
            while len(result.shots) < wanted:
                result.shots.append(result.shots[0])
            return result
        log(f"      (no still matched your query for {name}; trying clips)")

    if query and not depictable:
        found = _authored_shots(query, name, wanted, seen, log)
        if found:
            result = ItemVisuals(name=name, shots=found[:wanted],
                                 tier=config.TIER_ILLUSTRATIVE)
            while len(result.shots) < wanted:
                result.shots.append(result.shots[0])
            return result
        log(f"      (nothing matched your query for {name}; using the subject)")

    entity = entities.resolve(name, kind)
    if entity.error:
        # A throttled lookup is not a missing entity. On an unattended run this
        # is the difference between an item on its real subject and an item on
        # clouds, so it is worth one slow retry: an audit of ten billionaires
        # lost Musk, Ellison and Brin to exactly this, all three of which
        # resolve perfectly on their own.
        log(f"      ! wikidata: {entity.error} — retrying once")
        time.sleep(5)
        entity = entities.resolve(name, kind)
    result = ItemVisuals(name=name, entity=entity)
    if entity.error:
        log(f"      ! wikidata still unreachable: {entity.error}")

    # Rung 1: the subject itself. Stock video first when it verifies, because
    # real motion beats a moving photograph; Commons carries everything else.
    shots = _stock_shots(entity, wanted, seen, log, query)
    if len(shots) < wanted:
        shots += _commons_shots(entity, wanted - len(shots), seen, log, day_dir)
    if shots:
        result.shots = shots[:wanted]
        result.tier = config.TIER_EXACT
        # One authentic shot is enough to carry the item: repeat it rather
        # than pad with something that is not the subject.
        while len(result.shots) < wanted:
            result.shots.append(result.shots[len(result.shots) % len(shots)])
        return result

    # A depictable item that still found nothing of itself may fall back to the
    # author's query before dropping to a proxy -- the query at least describes
    # the right idea.
    if query and depictable:
        found = _authored_shots(query, name, wanted, seen, log)
        if found:
            result.shots = found[:wanted]
            result.tier = config.TIER_ILLUSTRATIVE
            while len(result.shots) < wanted:
                result.shots.append(result.shots[0])
            return result

    # Rung 2: a real, related entity.
    for proxy_name, _qid in (entity.proxies or []):
        proxy = entities.resolve(proxy_name, "")
        log(f"      no footage of {name}; trying proxy '{proxy_name}'")
        proxy_shots = _stock_shots(proxy, wanted, seen, log)
        if len(proxy_shots) < wanted:
            proxy_shots += _commons_shots(proxy, wanted - len(proxy_shots),
                                          seen, log, day_dir)
        if proxy_shots:
            for shot in proxy_shots:
                shot.tier = config.TIER_PROXY
                shot.note = f"proxy for {name}"
            result.shots = proxy_shots[:wanted]
            while len(result.shots) < wanted:
                result.shots.append(result.shots[0])
            result.tier = config.TIER_PROXY
            return result

    # Rung 3: nothing verified. Mood footage, recorded as such -- the build
    # reports this loudly because a numbered item on generic footage is the
    # failure this project exists to avoid.
    log(f"      ! NOTHING authentic for '{name}' — falling back to mood footage")
    result.shots = generic(wanted, seen, log)
    result.tier = config.TIER_GENERIC
    return result


def audit_item(name: str, kind: str, seen: set | None = None,
               query: str = "", depictable: bool = False) -> dict:
    """What tier this item could reach, WITHOUT downloading anything.

    Search results and Commons metadata are cheap; speech and Ken Burns renders
    are not. Checking first means a topic whose items cannot be carried is
    known in seconds rather than after a six-minute build.
    """
    seen = seen or set()
    report = {"name": name, "qid": "", "label": "",
              "tier": config.TIER_GENERIC, "via": "", "detail": ""}
    # Your query decides first, unless the subject is recognisable -- see
    # for_item.
    if query and not depictable:
        for phrase in [x.strip() for x in re.split(r"\s+or\s+", query) if x.strip()]:
            try:
                _b, passing = stock.probe([phrase], [_query_tokens(phrase)], seen,
                                          min_ratio=config.ILLUSTRATIVE_MATCH_RATIO)
            except stock.StockError:
                passing = 0
            if passing:
                report.update(tier=config.TIER_ILLUSTRATIVE, via="your query",
                              detail=f"{passing} clip(s) for '{phrase[:30]}'")
                return report
    entity = entities.resolve(name, kind)
    report.update(qid=entity.qid, label=entity.label)
    if entity.error:
        report["detail"] = entity.error
        return report

    try:
        best, passing = stock.probe(entity.search_terms(), entity.identifiers(),
                                    seen)
    except stock.StockError:
        best, passing = 0.0, 0
    if passing:
        report.update(tier=config.TIER_EXACT, via="stock",
                      detail=f"{passing} verified clip(s)")
        return report

    try:
        media = commons.entity_media(entity)
    except Exception:  # noqa: BLE001
        media = []
    if media:
        report.update(tier=config.TIER_EXACT, via="commons",
                      detail=f"{len(media)} photo(s), best "
                             f"{media[0]['title'].replace('File:', '')[:40]}")
        return report

    for proxy_name, _qid in (entity.proxies or [])[:3]:
        proxy = entities.resolve(proxy_name, "")
        try:
            _best, passing = stock.probe(proxy.search_terms(),
                                         proxy.identifiers(), seen)
        except stock.StockError:
            passing = 0
        proxy_media = []
        if not passing:
            try:
                proxy_media = commons.entity_media(proxy)
            except Exception:  # noqa: BLE001
                proxy_media = []
        if passing or proxy_media:
            report.update(tier=config.TIER_PROXY, via=proxy_name,
                          detail="stock" if passing else "commons")
            return report

    report["detail"] = f"nothing verified (best stock match {best:.2f})"
    return report


def generic(count: int, seen: set, log) -> list:
    """Mood footage for beats with no subject of their own."""
    shots = []
    for phrase in config.GENERIC_VISUALS:
        if len(shots) >= count:
            break
        found = stock.find([phrase], set(), seen=seen, screen=_screen())
        if not found:
            continue
        path, credit = found
        shots.append(Shot(path=path, credit=credit, tier=config.TIER_GENERIC,
                          subject=phrase, note="mood footage"))
    if not shots:
        log("      ! no mood footage available at all")
    while shots and len(shots) < count:
        shots.append(shots[0])
    return shots
