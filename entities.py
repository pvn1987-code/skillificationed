"""Resolves an item name to a real Wikidata entity, and to what it looks like.

This module exists because keyword search cannot tell you whether a clip is of
the thing you asked for. Wikidata can: an entity has an identity (a Q-number),
a type, and -- crucially -- media that Wikidata itself attaches to it. A file
reached that way is of the subject by construction, not by ranking.

Three jobs:

1. RESOLVE a written name to one entity, using the item's expected kind to
   settle ambiguity. "Niagara Falls" is both a waterfall (Q34221) and a city in
   Ontario (Q274120); a travel list means the waterfall, and picking the wrong
   one poisons every search that follows.

2. Expose the entity's DISTINCTIVE WORDS -- the tokens that must appear in a
   stock clip's own metadata before that clip may be called authentic. For
   "Niagara Falls" that is {niagara}: "falls" alone matches any waterfall on
   earth, which is exactly the substitution this project is built to prevent.

3. Offer PROXIES: real, related entities to fall back to when the subject has
   no usable footage of its own. Elon Musk -> Tesla, SpaceX. A proxy is still
   something true about the item; it is never a stand-in dressed up as the
   subject, and it is recorded as a proxy in the manifest.

Wikidata and Commons are free and unmetered, but every lookup is cached on disk
anyway: a rebuild after a render bug should cost no network at all.
"""
import json
import re
import time
from dataclasses import dataclass, field, asdict

import requests

import config

_API = "https://www.wikidata.org/w/api.php"
_TIMEOUT = 20
_CACHE_FILE = config.STATE_DIR / "entities.json"
# Bumped whenever resolution logic changes. Without it a fix to disambiguation
# is invisible for every name already cached -- the "ward area of Tokyo" bug
# survived its own fix until this existed.
_CACHE_VERSION = 4

# Wikidata property numbers, named so the calls below read as English.
P_IMAGE = "P18"
P_VIDEO = "P10"
P_COMMONS_CAT = "P373"
P_INSTANCE_OF = "P31"
P_SUBCLASS_OF = "P279"
P_EMPLOYER = "P108"
P_OWNER_OF = "P1830"
P_NOTABLE_WORK = "P800"
P_PRODUCT = "P1056"
P_FOUNDED_BY = "P112"
P_COUNTRY = "P17"
# An administrative area's seat. A county is not a photographable thing -- every
# stock library and half of Commons offers only its courthouse -- but its county
# seat is a real town somebody has filmed.
P_CAPITAL = "P36"

# Q-numbers whose meaning the resolver needs to know by name.
Q_HUMAN = "Q5"

# Type words that describe a CATEGORY rather than identify a thing. A stock
# clip matching only these has matched nothing: "falls" is every waterfall,
# "city" is every city. Stripped before a name becomes a verification test.
_GENERIC_TOKENS = {
    "the", "a", "an", "of", "in", "on", "at", "and", "de", "la", "el",
    "falls", "waterfall", "waterfalls", "city", "town", "village", "island",
    "islands", "mountain", "mountains", "lake", "river", "sea", "ocean",
    "beach", "park", "national", "state", "temple", "church", "cathedral",
    "mosque", "palace", "castle", "tower", "bridge", "museum", "monument",
    "statue", "square", "street", "road", "valley", "desert", "forest",
    "canyon", "glacier", "volcano", "reef", "bay", "coast", "harbour",
    "harbor", "inc", "corp", "corporation", "company", "ltd", "llc", "group",
    "holdings", "sa", "se", "plc", "co", "jr", "sr", "mount", "mt", "st",
    "saint", "new", "old", "great", "grand",
}

# How a kind hint from the planner maps onto what Wikidata says a thing is.
# Checked against the entity's own description text as well as its P31, since
# the description ("American businessman", "three waterfalls that straddle...")
# is often the faster discriminator and always present.
_KIND_WORDS = {
    "person": ("human", "person", "businessman", "businesswoman", "singer",
               "politician", "actor", "actress", "entrepreneur", "footballer",
               "athlete", "author", "artist", "scientist", "investor"),
    "place": ("city", "town", "country", "capital", "municipality", "region",
              "state", "province", "island", "village", "metropolis"),
    "landmark": ("waterfall", "mountain", "temple", "monument", "building",
                 "ruins", "site", "park", "lake", "river", "castle", "palace",
                 "tower", "cathedral", "archaeological", "volcano", "canyon"),
    "organisation": ("company", "corporation", "business", "manufacturer",
                     "enterprise", "organization", "organisation", "brand",
                     "bank", "club", "team", "studio"),
    "product": ("model", "vehicle", "car", "aircraft", "device", "software",
                "smartphone", "product", "spacecraft", "rocket"),
    "work": ("film", "movie", "album", "song", "book", "novel", "series",
             "video game", "television"),
}


@dataclass
class Entity:
    """One resolved subject, with everything needed to find real footage."""
    name: str                      # as written by the planner
    qid: str = ""
    label: str = ""
    description: str = ""
    kind: str = ""                 # the hint that won, or "" if unresolved
    image: str = ""                # Commons filename from P18
    video: str = ""                # Commons filename from P10, rare but gold
    commons_cat: str = ""
    country: str = ""
    proxies: list = field(default_factory=list)   # [(name, qid)]
    error: str = ""               # set when the lookup itself failed

    @property
    def resolved(self) -> bool:
        return bool(self.qid)

    @staticmethod
    def _tokens(source: str) -> set:
        """Identifying words of a name, minus the ones that name a category.

        Falls back to every token when a name is *entirely* generic words, e.g.
        "Great Barrier Reef" -> {barrier}; an empty set would make the test
        vacuous and let anything through, which is the one outcome worse than
        being too strict.
        """
        tokens = {t for t in re.split(r"\W+", source.lower()) if len(t) > 2}
        return (tokens - _GENERIC_TOKENS) or tokens

    def distinctive(self) -> set:
        """Tokens a clip's own metadata must contain to count as this subject.

        Taken from the name AS WRITTEN, never from the resolved label alone. A
        live build resolved "Tokyo" to Q308891, whose label is "ward area of
        Tokyo"; keying off that label demanded clips whose slug said *ward* and
        *area* as well, so genuine Tokyo footage scored 0.33 and the most
        photographed city on earth fell through to mood footage.

        The rule is that a bad resolution must never be able to TIGHTEN the
        gate. See identifiers() for how a legitimately different label is still
        allowed to satisfy it.
        """
        return self._tokens(self.name or self.label)

    def identifiers(self) -> list:
        """Every token set that legitimately identifies this subject.

        The name as written, plus the Wikidata label when it is genuinely a
        different way of saying the same thing ("NYC" / "New York City"). A
        clip satisfies the gate by matching ANY of these -- so an alternative
        name can only ever widen what counts as authentic, never narrow it.
        """
        sets = [self.distinctive()]
        # "Kyoto, Japan" and "Salar de Uyuni, Bolivia" qualify a name with its
        # country. Stock slugs rarely repeat the country, so demanding every
        # token would reject the right clip -- the same way "ward area of
        # Tokyo" once did. The core name before the comma counts too.
        head = (self.name or "").split(",")[0].strip()
        if head and head != (self.name or "").strip():
            head_tokens = self._tokens(head)
            if head_tokens and head_tokens not in sets:
                sets.append(head_tokens)
        if self.label:
            label_tokens = self._tokens(self.label)
            if label_tokens and label_tokens not in sets:
                sets.append(label_tokens)
        return sets

    def search_terms(self) -> list:
        """Stock-library phrasings to try, best first.

        The bare name first, then the name qualified by its country, which is
        what rescues a place whose name is ambiguous in the library even though
        it was unambiguous in Wikidata.
        """
        name = self.label or self.name
        terms = [name]
        if self.country and self.country.lower() not in name.lower():
            terms.append(f"{name} {self.country}")
        return terms


# --- plumbing ---------------------------------------------------------------

_cache = None


def _load_cache() -> dict:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(_CACHE_FILE.read_text())
        except (OSError, ValueError):
            _cache = {}
    return _cache


def _save_cache() -> None:
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(json.dumps(_cache, indent=2))
    except (OSError, TypeError):
        pass  # A cache that will not write is a slowdown, never a failure.


_session = None
_last_call = [0.0]
# Wikidata throttles bursts: ten items resolved back to back is thirty calls in
# a couple of seconds, and a live build took a 429 on item six. Spacing them is
# cheaper than retrying them, and the cache means it is paid once per entity.
_MIN_INTERVAL = [0.35]


def _http():
    """One pooled session, so a run of ten items is ten requests on one socket."""
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({"User-Agent": config.WIKI_USER_AGENT})
    return _session


def _get(params: dict, attempts: int = 4) -> dict:
    """A Wikidata call, retried on the transient failures it actually returns.

    Written after a first pass resolved three names and dropped two: burst
    traffic from one run was being throttled, the exception was swallowed, and
    the empty result was then CACHED -- so a momentary blip turned into a
    permanently unresolvable entity. Retry here, and let a genuine failure
    raise so the caller can decline to cache it.
    """
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
                wait = float(response.headers.get("Retry-After") or (attempt + 1) * 2)
                time.sleep(min(wait, 10))
                last = RuntimeError(f"wikidata {response.status_code}")
                continue
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            last = exc
            time.sleep((attempt + 1) * 1.5)
    raise RuntimeError(f"wikidata unreachable after {attempts} tries: {last}")


def _search(name: str, limit: int = 5) -> list:
    data = _get({"action": "wbsearchentities", "search": name,
                 "language": "en", "uselang": "en", "limit": limit})
    return [{"qid": hit["id"], "label": hit.get("label", ""),
             "description": hit.get("description", "")}
            for hit in data.get("search", [])]


def _claims(qid: str) -> dict:
    return _get({"action": "wbgetclaims", "entity": qid}).get("claims", {})


def _value(claims: dict, prop: str):
    for claim in claims.get(prop, []):
        value = claim.get("mainsnak", {}).get("datavalue", {}).get("value")
        if value:
            return value
    return None


def _item_ids(claims: dict, prop: str, limit: int = 3) -> list:
    out = []
    for claim in claims.get(prop, [])[:limit]:
        value = claim.get("mainsnak", {}).get("datavalue", {}).get("value")
        if isinstance(value, dict) and value.get("id"):
            out.append(value["id"])
    return out


def _labels(qids: list) -> dict:
    """Labels for several Q-numbers in one call."""
    if not qids:
        return {}
    data = _get({"action": "wbgetentities", "ids": "|".join(qids),
                 "props": "labels", "languages": "en"})
    return {qid: (body.get("labels", {}).get("en", {}) or {}).get("value", "")
            for qid, body in (data.get("entities") or {}).items()}


# --- resolution -------------------------------------------------------------

def _kind_score(candidate: dict, kind_hint: str, name: str = "") -> int:
    """How well a search hit matches what was actually asked for.

    Two signals. The stronger by far is an EXACT label match: asked for
    "Tokyo", the entity labelled "Tokyo" is the one meant, not the entity
    labelled "ward area of Tokyo" that Wikidata's search happened to rank
    first. The weaker is the description text, which Wikidata writes precisely
    to disambiguate -- "three waterfalls that straddle the international
    border" scores for `landmark`, "city located in the province of Ontario"
    does not.
    """
    score = 0
    label = (candidate.get("label") or "").strip().lower()
    asked = (name or "").strip().lower()
    if asked and label == asked:
        score += 10
    elif asked and asked.startswith(label) and len(label) > 4:
        # The asked-for name QUALIFIES the label: "Santa Clara County,
        # California" against the entity labelled "Santa Clara County". Without
        # this, that name resolved to Q59497069 -- "Santa Clara County,
        # California Veterans Services Office Collection (NAID 17614838)", an
        # archival record series with no photographs at all.
        score += 6
    elif asked and label.startswith(asked):
        score += 3
    # Archival collections, court cases and datasets have long descriptive
    # labels and are never what a list item means.
    if len(label.split()) > len(asked.split()) + 3:
        score -= 4
    if kind_hint:
        words = _KIND_WORDS.get(kind_hint, ())
        text = f"{candidate.get('description', '')}".lower()
        score += sum(2 for word in words if word in text)
    return score


def resolve(name: str, kind_hint: str = "", fetch_proxies: bool = True) -> Entity:
    """One written name to one Wikidata entity. Never raises on a miss.

    An unresolved name is not an error: plenty of valid list items ("a Tuesday
    in November") are not entities at all. The caller treats an unresolved
    entity as "verify by slug text only".
    """
    key = f"v{_CACHE_VERSION}|{name.lower()}|{kind_hint}|{int(fetch_proxies)}"
    cache = _load_cache()
    if key in cache:
        return Entity(**cache[key])

    entity = Entity(name=name, kind=kind_hint)
    try:
        candidates = _search(name)
    except RuntimeError as exc:
        # Deliberately NOT cached: an unreachable API is a condition of this
        # minute, not a fact about the entity.
        entity.error = str(exc)
        return entity
    if not candidates and "," in name:
        # "Salar de Uyuni, Bolivia" finds nothing, while "Salar de Uyuni" is
        # Wikidata's actual title. Without this the entity never resolves, so
        # Commons is never consulted -- and because no stock clip can pass the
        # gate for a Bolivian salt flat either (the library offers Utah), the
        # item silently drops to mood footage despite excellent photographs of
        # it existing.
        head = name.split(",")[0].strip()
        if head and head.lower() != name.lower():
            try:
                candidates = _search(head)
            except RuntimeError:
                candidates = []
    if not candidates:
        cache[key] = asdict(entity)
        _save_cache()
        return entity

    # Wikidata's own ranking is a good prior, so the kind hint only has to
    # break ties -- hence position acting as a small penalty rather than the
    # score being purely semantic.
    best = max(enumerate(candidates),
               key=lambda pair: _kind_score(pair[1], kind_hint, name)
               - pair[0] * 0.5)[1]
    entity.qid = best["qid"]
    entity.label = best["label"] or name
    entity.description = best["description"]

    try:
        claims = _claims(entity.qid)
    except RuntimeError as exc:
        entity.error = str(exc)
        return entity  # Half-resolved, uncached: retry properly next run.

    image = _value(claims, P_IMAGE)
    video = _value(claims, P_VIDEO)
    commons = _value(claims, P_COMMONS_CAT)
    entity.image = image if isinstance(image, str) else ""
    entity.video = video if isinstance(video, str) else ""
    entity.commons_cat = commons if isinstance(commons, str) else ""

    country_ids = _item_ids(claims, P_COUNTRY, limit=1)
    is_human = Q_HUMAN in _item_ids(claims, P_INSTANCE_OF, limit=5)
    if is_human and not entity.kind:
        entity.kind = "person"

    if fetch_proxies:
        # A person's employers and companies, a company's products: each is a
        # real entity with its own verifiable footage, which is what makes the
        # fallback authentic rather than decorative.
        proxy_ids = []
        for prop in (P_CAPITAL, P_OWNER_OF, P_EMPLOYER, P_NOTABLE_WORK,
                     P_PRODUCT, P_FOUNDED_BY):
            proxy_ids += _item_ids(claims, prop, limit=2)
        # Deduplicate while keeping the property order, which runs most
        # strongly associated first.
        seen, ordered = set(), []
        for qid in proxy_ids:
            if qid not in seen:
                seen.add(qid)
                ordered.append(qid)
        try:
            names = _labels(ordered[:4] + country_ids)
        except RuntimeError:
            names = {}  # Proxies are a nicety; losing them is not a failure.
        entity.proxies = [(names[qid], qid) for qid in ordered[:4]
                          if names.get(qid)]
        if country_ids:
            entity.country = names.get(country_ids[0], "")
        time.sleep(0.25)  # Courtesy to a free API, and it throttles bursts.

    cache[key] = asdict(entity)
    _save_cache()
    return entity


def resolve_all(items: list) -> list:
    """Resolve a planner's items in order. items = [{name, kind}, ...]."""
    return [resolve(item.get("name", ""), item.get("kind", ""))
            for item in items]
