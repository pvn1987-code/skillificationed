"""Gemini writes the countdown, in the SAME JSON shape a hand-written spec uses.

That shape is not a coincidence and it is the point: `spec.load_dict` ingests
both, so a generated reel and a hand-written one are rendered by identical code
and cannot drift apart. Anything learned from tuning a hand-written spec applies
to the generated ones for free.

The field that matters most is `pexels_query`. Without it the pipeline can only
ask "what IS this thing?", and for a subject nobody can recognise in a
photograph -- a county, a statistic -- the honest answer is its courthouse,
which is useless in a reel about wealth. With it, the model can say what shot
CONVEYS the item. `depictable` decides which of the two wins; see visuals.py.

Quota is the binding constraint: the free tier allows 20 requests a day. So
every response is cached on disk under a hash of the request, and a rebuild
after a render bug, a caption tweak or a footage swap costs nothing.

The prompt encodes the retention findings the format is built on, because the
script is where most of them are won or lost:

* The hook must pay off inside about two seconds -- attention spans on
  short-form measure around 2.1s and the first 1.7s is the retention window.
* No preamble. Filling the 3-15s window with throat-clearing costs 30-50%.
* Count DOWN: the unrevealed number one is the open loop carrying the video.
* One fresh open loop mid-way, because a list sags around the halfway mark.
* The word budget is a TIME budget -- the voice reads ~2.8 words a second and
  the reel has to land in the 45-60s band that measures best.
"""
import hashlib
import json
import re
import time

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

import config

SCHEMA_VERSION = 6          # bump to invalidate every cached plan
_WORDS_PER_SECOND = 2.8


class PlannerError(RuntimeError):
    pass


class QuotaExhausted(PlannerError):
    pass


_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "rank": {"type": "integer", "description": "Position in the countdown."},
        "place_name": {
            "type": "string",
            "description": "The item's REAL, SPECIFIC, SEARCHABLE name exactly as it "
                           "is normally written: 'Niagara Falls', 'Elon Musk', "
                           "'Loudoun County, Virginia'. It is looked up in Wikidata "
                           "to find genuine footage, so it must be a real named "
                           "thing, never a description and never a category.",
        },
        "kind": {
            "type": "string",
            "enum": ["person", "place", "landmark", "organisation", "product",
                     "work", "other"],
            "description": "What sort of thing it is, used to settle an ambiguous "
                           "name -- 'Niagara Falls' is both a waterfall and a city.",
        },
        "depictable": {
            "type": "boolean",
            "description": "TRUE if a viewer could RECOGNISE this exact thing in a "
                           "photograph: the Eiffel Tower, Elon Musk, Santorini, a "
                           "Ferrari. FALSE if they could not, however famous the "
                           "NAME is: a county, a statistic, an administrative "
                           "region, an industry, a company's revenue. Be honest. "
                           "Marking an unrecognisable thing TRUE puts its courthouse "
                           "or its logo on screen instead of something worth "
                           "watching.",
        },
        "on_screen_text": {
            "type": "string",
            "description": "What appears on the card. FIRST LINE is the short name, "
                           "three or four words. If the list has a NUMBER behind it "
                           "(income, height, net worth, population, year), put it on "
                           "a SECOND line after a newline, like "
                           "'Median Income: ~$143k' -- that second line is often the "
                           "most interesting thing on screen. Do NOT start with the "
                           "rank number; the card draws it.",
        },
        "voiceover": {
            "type": "string",
            "description": "The spoken line, INCLUDING the announcement of the "
                           "number: 'Number 10. Forsyth County, Georgia, a booming "
                           "and affluent suburb of Atlanta.' A HARD MAXIMUM of 18 "
                           "words including the number and the name. One concrete, "
                           "surprising, verifiable fact -- never a general "
                           "description. Plain prose for text-to-speech: no emojis, "
                           "markdown or symbols, and numbers written the way a "
                           "person says them.",
        },
        "pexels_query": {
            "type": "string",
            "description": "A stock-footage search phrase for THIS item, three to "
                           "six words. If depictable, name the thing itself "
                           "('Santorini Greece aerial ocean'). If NOT depictable, "
                           "describe the shot that CONVEYS it ('luxury suburban "
                           "neighborhood aerial', 'data centers'). Use DISTINCTIVE "
                           "words: a query made only of generic scenery words like "
                           "'beautiful park mountains' matches nothing usable. Never "
                           "name a famous place the item is not -- a recognisable "
                           "landmark under the wrong label is the one substitution "
                           "viewers catch.",
        },
        "duration_sec": {
            "type": "number",
            "description": "Roughly how long this item should be on screen, 2.5-4.5.",
        },
    },
    "required": ["rank", "place_name", "kind", "depictable", "on_screen_text",
                 "voiceover", "pexels_query", "duration_sec"],
}

_SCHEMA = {
    "type": "object",
    "properties": {
        "intro": {
            "type": "object",
            "properties": {
                "on_screen_text": {"type": "string",
                                   "description": "The list's title, under 8 words."},
                "voiceover": {
                    "type": "string",
                    "description": "The hook: the opening line and the most important "
                                   "one, with under two seconds to stop a thumb. "
                                   "9-13 words. Name the promise immediately and "
                                   "raise a stake or a surprise. No greeting, no "
                                   "channel name, no 'in this video', no warm-up "
                                   "question.",
                },
                "pexels_query": {"type": "string",
                                 "description": "Opening shot, three to six words."},
                "duration_sec": {"type": "number"},
            },
            "required": ["on_screen_text", "voiceover", "pexels_query",
                         "duration_sec"],
        },
        "items": {"type": "array", "items": _ITEM_SCHEMA},
        "tease": {
            "type": "string",
            "description": "One line, 6-10 words, played about halfway through, that "
                           "re-opens curiosity about number one without revealing it.",
        },
        "outro": {
            "type": "object",
            "properties": {
                "on_screen_text": {"type": "string",
                                   "description": "Closing card text, may use a "
                                                  "newline for a second line."},
                "voiceover": {"type": "string",
                              "description": "The closing line. Ask for a SHARE, a "
                                             "send or a comment -- never a like."},
                "pexels_query": {"type": "string"},
                "duration_sec": {"type": "number"},
            },
            "required": ["on_screen_text", "voiceover", "pexels_query",
                         "duration_sec"],
        },
        "caption": {
            "type": "string",
            "description": "Instagram caption: one or two lines, then 8-12 topical "
                           "hashtags. Separate lines with a newline.",
        },
        "sources_note": {
            "type": "string",
            "description": "One line on where these facts come from, flagging "
                           "anything contested or estimated.",
        },
    },
    "required": ["intro", "items", "tease", "outro", "caption", "sources_note"],
}

_TOPIC_SCHEMA = {
    "type": "object",
    "properties": {
        "topics": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string",
                              "description": "A countdown topic, phrased as it would "
                                             "be said: 'Top 10 cities to visit "
                                             "before you die'."},
                    "why": {"type": "string",
                            "description": "One line on why this would hold attention."},
                    "footage_risk": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                        "description": "How hard authentic footage will be to find. "
                                       "Famous places, landmarks and public figures "
                                       "are low. Administrative regions, statistics "
                                       "and abstractions are high -- nobody can "
                                       "recognise a county in a photograph.",
                    },
                },
                "required": ["topic", "why", "footage_risk"],
            },
        },
    },
    "required": ["topics"],
}

_SWAP_SCHEMA = {
    "type": "object",
    "properties": {
        "replacements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "replaces": {"type": "string",
                                 "description": "The item being replaced, exactly."},
                    "place_name": {"type": "string",
                                   "description": "A real, famous, VISUALLY "
                                                  "RECOGNISABLE named entity that "
                                                  "fits the same list."},
                    "kind": {"type": "string",
                             "enum": ["person", "place", "landmark", "organisation",
                                      "product", "work", "other"]},
                    "depictable": {"type": "boolean"},
                    "on_screen_text": {"type": "string"},
                    "voiceover": {"type": "string",
                                  "description": "Same hard word limit as the "
                                                 "original, number included."},
                    "pexels_query": {"type": "string"},
                },
                "required": ["replaces", "place_name", "kind", "depictable",
                             "on_screen_text", "voiceover", "pexels_query"],
            },
        },
    },
    "required": ["replacements"],
}

_FORMAT_RULES = """FORMAT — measured constraints, not preferences. The length rules
are HARD LIMITS and the most common way these scripts fail is by running long.

LENGTH
- Each item's `voiceover` must be AT MOST {per_item} words INCLUDING the number and the
  name it announces. Count them. A line of {over} words is a failure however good it is.
- The whole script must come to about {budget} words, reading aloud in {lo:.0f}-{hi:.0f}
  seconds.

THE FACT
- One concrete, surprising, checkable fact per item: a number, a record, a superlative,
  an unexpected detail. Something a viewer would repeat to someone else.
- Brochure adjectives are banned: vibrant, iconic, bustling, breathtaking, stunning,
  world-class, captivating, unforgettable, must-see, nestled, charming. They fill the
  word budget and say nothing.
- BAD  : "Its thousands of temples and gardens captivate visitors." (says nothing)
  GOOD : "Seventeen of its temples are UNESCO sites."
- Never invent facts, numbers or rankings. If a ranking is contested or an estimate,
  say so in sources_note and keep the spoken line defensible.

THE HOOK
- It has under two seconds to stop a thumb and is the highest-leverage line in the
  script. State the promise AND a stake or a surprise, immediately.
- No greeting, no "welcome back", no "in this video", no "ready to explore", no
  warm-up question. Viewers leave during preamble.
- BAD  : "Ready to explore? These ten places promise unforgettable memories."
  GOOD : "Most people die having seen four of these ten cities."

STRUCTURE
- The countdown runs from {first} DOWN to 1. Number one is the payoff the whole video
  withholds, so it must be the strongest entry, not merely the most famous.

VOICE
- Write for the ear, for a text-to-speech narrator. No brackets, symbols, emojis,
  markdown or stage directions. Spell numbers and currencies the way a person says
  them: "two point six billion dollars", "seventeen".
"""

_VISUAL_RULES = """VISUALS — every item also needs a shot, and this is where these
reels usually fall down.

- Set `depictable` honestly. A viewer recognises the Eiffel Tower, Elon Musk and
  Santorini in a photograph. They do NOT recognise Loudoun County, a median income, or
  "the semiconductor industry", however famous those names are.
- For a depictable item, `pexels_query` should name the thing: "Santorini Greece aerial
  ocean". Verified footage of the real subject will be preferred over your query.
- For an item that is NOT depictable, your query IS the shot, so make it convey the
  idea: "luxury suburban neighborhood aerial" for a wealthy county, "data centers" for
  a county full of them.
- Use DISTINCTIVE words. A query of only generic scenery words -- "beautiful suburban
  park maryland" -- matches nothing usable and the item falls back to something dull.
- Never name a famous place the item is not. A recognisable landmark shown under the
  wrong label is the one substitution viewers actually catch.
- For a NOT-depictable item, describe what that place DOES, not the landscape it sits
  in. Stock libraries hold the most photogenic version of any landscape, and it is
  almost always somewhere iconic and somewhere else. "Loudoun County Virginia vineyards"
  returned TUSCANY -- cypresses, olive groves, an Italian villa -- captioned LOUDOUN
  COUNTY, VA. "data centers" would have been unmistakable and unmistakably right.
  Prefer the distinctive activity: data centers, office parks, defense contractors,
  container ports, suburban mansions. Avoid bare landscape nouns: vineyards, mountains,
  countryside, valley, coastline.
- `on_screen_text`: short name on the first line; if the list is built on a number, put
  it on a second line. That figure is often the most interesting thing on screen.
"""

_PROMPT = """You are writing a vertical countdown reel for a short-form video channel.

TOPIC: {topic}

Write a {count}-item countdown.

{rules}

{visuals}

CRITICAL — every `place_name` is looked up in Wikidata to find real footage, so it must
be a specific, real, well-known named entity a reference work would have an article
about. "Niagara Falls" works. "A waterfall in Canada" does not.
"""

_ITEMS_GIVEN_PROMPT = """You are writing a vertical countdown reel for a short-form
video channel.

TOPIC: {topic}

The items are already decided. Use these EXACTLY, in this order, and do not add,
remove, reorder or rename any of them:

{items}

Write the voiceover, on-screen text and shot for each, plus the hook, the mid-roll
tease, the outro and the caption.

{rules}

{visuals}
"""

_SWAP_PROMPT = """These items in a "{topic}" countdown have NO usable footage of them
anywhere -- no stock clip names them and no reference photograph of them exists:

{failures}

Replace each with a different entry that genuinely belongs in the same list AND is
visually recognisable: somewhere or someone a viewer would identify on sight, and that
photographers actually shoot. Keep the ranking honest -- do not promote an item that
does not deserve its place just because it photographs well.

Do not reuse any of these, already in the list:
{keep}

{rules}

{visuals}
"""

_TOPICS_PROMPT = """Suggest {count} countdown-reel topics for a vertical short-form
video channel that covers interesting factual lists.

Good topics are visually concrete: every item must be a real, famous, named thing that
genuine footage or a well-known photograph exists of. Rate footage_risk HIGH for topics
whose items are administrative regions, statistics, industries or abstractions -- a
viewer cannot recognise a county or a market share in a photograph. Vary the subjects.
"""


def _cache_key(*parts) -> str:
    raw = "|".join(str(p) for p in parts) + f"|v{SCHEMA_VERSION}"
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
        (config.PLAN_CACHE / f"{key}.json").write_text(json.dumps(payload, indent=2))
    except OSError:
        pass


def _ask(prompt: str, schema: dict, temperature: float = 0.9,
         max_retries: int = 2) -> dict:
    """One Gemini call, with the retry policy the free tier actually needs."""
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
                    "Gemini free-tier quota exhausted (20 requests/day on "
                    f"{config.GEMINI_MODEL}). Cached plans still build offline."
                ) from exc
            time.sleep(int(delay.group(1)) + 2)
    raise QuotaExhausted("Gemini quota exhausted")


def _rules(count: int) -> str:
    mid = (config.TARGET_SECONDS_MIN + config.TARGET_SECONDS_MAX) / 2
    budget = int(mid * _WORDS_PER_SECOND)
    # Unlike the old schema, the voiceover now INCLUDES the number and name, so
    # the per-item budget is the whole spoken line rather than what is left
    # after them.
    per_item = max(int((budget - 30) / max(count, 1)), 8)
    return _FORMAT_RULES.format(
        lo=config.TARGET_SECONDS_MIN, hi=config.TARGET_SECONDS_MAX,
        budget=budget, per_item=per_item, over=per_item + 5,
        first=count if config.COUNT_DOWN else 1)


def plan(topic: str, count: int | None = None, items: list | None = None,
         refresh: bool = False) -> dict:
    """A countdown spec for a topic, in the hand-written spec's own shape.

    Cached; only `refresh` spends quota.
    """
    count = count or config.ITEM_COUNT
    key = _cache_key("spec", topic, count, tuple(items or ()))
    if not refresh:
        hit = _cached(key)
        if hit:
            hit["_cached"] = True
            return hit

    if items:
        prompt = _ITEMS_GIVEN_PROMPT.format(
            topic=topic, rules=_rules(len(items)), visuals=_VISUAL_RULES,
            items="\n".join(f"{i + 1}. {name}" for i, name in enumerate(items)))
    else:
        prompt = _PROMPT.format(topic=topic, count=count,
                                rules=_rules(count), visuals=_VISUAL_RULES)

    result = _ask(prompt, _SCHEMA)
    result["topic"] = topic
    result["_cached"] = False
    _validate(result, count if not items else len(items))
    _store(key, result)
    return result


def suggest_topics(count: int = 8, refresh: bool = False) -> list:
    """Topic ideas, ranked by how findable authentic footage will be."""
    key = _cache_key("topics", count)
    if not refresh:
        hit = _cached(key)
        if hit:
            return hit.get("topics", [])
    result = _ask(_TOPICS_PROMPT.format(count=count), _TOPIC_SCHEMA,
                  temperature=1.0)
    _store(key, result)
    return result.get("topics", [])


def swap_items(topic: str, failures: list, keep: list, count: int,
               refresh: bool = False) -> dict:
    """Replacements for items that cannot be carried. {old name: new item}."""
    if not failures:
        return {}
    key = _cache_key("swap", topic, tuple(sorted(failures)), tuple(sorted(keep)))
    if not refresh:
        hit = _cached(key)
        if hit:
            return {r["replaces"]: r for r in hit.get("replacements", [])}
    result = _ask(_SWAP_PROMPT.format(
        topic=topic, failures="\n".join(f"- {f}" for f in failures),
        keep=", ".join(keep) or "(none)", rules=_rules(count),
        visuals=_VISUAL_RULES), _SWAP_SCHEMA)
    _store(key, result)
    return {r["replaces"]: r for r in result.get("replacements", [])}


def _validate(spec_dict: dict, expected: int) -> None:
    """Catch the schema-shaped failures before they reach the renderer."""
    items = spec_dict.get("items") or []
    if not items:
        raise PlannerError("Gemini returned no items")
    if len(items) != expected:
        # Not fatal: a short list renders fine and padding it would be worse.
        spec_dict.setdefault("warnings", []).append(
            f"asked for {expected} items, got {len(items)}")
    for item in items:
        if not item.get("place_name"):
            raise PlannerError("an item came back with no place_name")
        item.setdefault("on_screen_text", item["place_name"])
        item.setdefault("kind", "other")
        item.setdefault("pexels_query", "")
    # Renumber defensively: the numbers are spoken AND drawn, so a gap or a
    # duplicate from the model would desync the voice from the card.
    ordered = sorted(items, key=lambda i: -int(i.get("rank") or 0)
                     if config.COUNT_DOWN else int(i.get("rank") or 0))
    total = len(ordered)
    for position, item in enumerate(ordered):
        item["rank"] = total - position if config.COUNT_DOWN else position + 1
    spec_dict["items"] = ordered


def script_words(spec_dict: dict) -> int:
    """Rough spoken length of a spec, for the budget warning."""
    text = " ".join(
        [(spec_dict.get("intro") or {}).get("voiceover", ""),
         spec_dict.get("tease", ""),
         (spec_dict.get("outro") or {}).get("voiceover", "")]
        + [i.get("voiceover", "") for i in spec_dict.get("items", [])])
    return len(text.split())
