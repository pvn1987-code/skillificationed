#!/usr/bin/env python3
"""ReelForge — countdown reels built on verified footage of the real subject.

  ./reelforge.py topics                      ideas, ranked by footage risk
  ./reelforge.py plan "Top 10 cities to visit before you die"
  ./reelforge.py plan "..." --items items.txt    your own list, one per line
  ./reelforge.py build "..."                 render the plan (edit it first)
  ./reelforge.py make "..."                  plan and build in one go
  ./reelforge.py probe                       environment check, no network
  ./reelforge.py prune                       evict oldest cached assets

  ./reelforge.py remix "how to fold a fitted sheet"       skill reel
  ./reelforge.py remix-plan "..." --mode topic --topic "Top 10 cities"
  ./reelforge.py remix-build "..."           render the remix plan

Plans are cached, so `build` after editing plan.json spends no Gemini quota.

Exit codes: 0 ok, 1 nothing to do, 2 dependency missing, 3 unexpected,
5 rate limited (retry later).
"""
import argparse
import json
import os
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path

# This module needs the light venv (requests, Pillow, dotenv, google-genai).
# The heavy TTS/ASR venv is only ever entered by reel_worker.py as a subprocess.
# A bare ./reelforge.py picks up the system python3 from the shebang and dies on
# `import config`, so re-exec under the right interpreter instead of failing.
# Compare sys.prefix, not sys.executable: .venv/bin/python is a symlink to the
# system interpreter, so resolving both collapses them to one path.
_VENV = Path(__file__).resolve().parent / ".venv"
_VENV_PYTHON = _VENV / "bin" / "python"
if (_VENV_PYTHON.exists() and not os.environ.get("REELFORGE_REEXEC")
        and Path(sys.prefix).resolve() != _VENV.resolve()):
    os.execve(str(_VENV_PYTHON),
              [str(_VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]],
              {**os.environ, "REELFORGE_REEXEC": "1"})

import build as builder
import config
import planner
import remix
import spec as specfile
import voiceclone
from sources import stock, youtube


def _log(message: str = "") -> None:
    print(message, flush=True)
    try:
        with config.LOG_FILE.open("a") as handle:
            handle.write(message + "\n")
    except OSError:
        pass  # Logging must never be the thing that fails a build.


def _slug(topic: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")
    return text[:48] or "reel"


def _day_dir(topic: str) -> Path:
    return config.OUTPUT_DIR / _slug(topic)


def _plan_path(topic: str) -> Path:
    return _day_dir(topic) / "plan.json"


def _remix_day_dir(query: str) -> Path:
    # Prefixed so a remix reel can never collide with a countdown slug built
    # from a similar-sounding query.
    return config.OUTPUT_DIR / f"remix-{_slug(query)}"


def _remix_plan_path(query: str) -> Path:
    return _remix_day_dir(query) / "remix_plan.json"


def cmd_topics(args) -> int:
    ideas = planner.suggest_topics(args.count, refresh=args.refresh)
    if not ideas:
        _log("no topics returned")
        return 1
    _log("\nTopic ideas (footage risk = how hard authentic shots will be):\n")
    order = {"low": 0, "medium": 1, "high": 2}
    for idea in sorted(ideas, key=lambda i: order.get(i.get("footage_risk"), 3)):
        _log(f"  [{idea.get('footage_risk', '?'):6}] {idea.get('topic', '')}")
        _log(f"           {idea.get('why', '')}")
    _log("\nLow risk means every item is a famous, visually recognisable thing.")
    return 0


def cmd_plan(args) -> int:
    """Write the script. The planner emits the SAME JSON shape a hand-written
    spec uses, and it goes through the same loader, so a generated reel and a
    hand-written one are rendered by identical code."""
    items = None
    if args.items:
        source = Path(args.items)
        if not source.exists():
            _log(f"X no such file: {source}")
            return 1
        items = [line.strip() for line in source.read_text().splitlines()
                 if line.strip()]
        _log(f"  using your {len(items)} items from {source.name}")

    raw = planner.plan(args.topic, count=args.count, items=items,
                       refresh=args.refresh)
    plan = specfile.load_dict(raw, source=raw.get("sources_note", ""))
    day = _day_dir(args.topic)
    day.mkdir(parents=True, exist_ok=True)
    # Both are written: spec.json is the editable source in the same format you
    # would hand-write, plan.json is what the builder reads.
    (day / "spec.json").write_text(json.dumps(raw, indent=2))
    (day / "plan.json").write_text(json.dumps(plan, indent=2))

    _log(f"\n  {plan.get('title', '')}"
         + ("   (from cache — no quota spent)" if raw.get("_cached") else ""))
    _log(f"\n  HOOK: \"{plan.get('hook', '')}\"")
    for item in plan.get("items", []):
        mark = "" if item.get("depictable") else "  [not recognisable — shot is the query]"
        _log(f"   {item['rank']:>2}. {item['name'][:34]:36}{mark}")
        _log(f"       say : {item['line']}")
        _log(f"       card: {item['caption']}".replace("\n", " / "))
        _log(f"       shot: {item.get('query', '')}")
    if plan.get("tease"):
        _log(f"\n  TEASE: \"{plan['tease']}\"")
    _log(f"  CTA:   \"{plan.get('cta', '')}\"")
    words = planner.script_words(raw)
    # Gemini always writes English here (see planner.WORDS_PER_SECOND), so
    # this deliberately does not use config.WORDS_PER_SECOND -- that reflects
    # whatever language the build's own voice is configured for, which is a
    # different, unrelated setting from what language THIS text is written in.
    _log(f"\n  {words} words, about {words / planner.WORDS_PER_SECOND:.0f}s spoken "
         f"(target {config.TARGET_SECONDS_MIN:.0f}-{config.TARGET_SECONDS_MAX:.0f}s)")
    if plan.get("sources_note"):
        _log(f"  note: {plan['sources_note']}")
    for warning in raw.get("warnings", []):
        _log(f"  ! {warning}")
    _log(f"\n  written to {day / 'spec.json'} (edit this) and plan.json")
    _log(f"  then:  ./reelforge.py build \"{args.topic}\"")
    return 0


def cmd_build(args) -> int:
    path = _plan_path(args.topic)
    if not path.exists():
        _log(f"X no plan for '{args.topic}' — run `plan` first")
        return 1
    plan = json.loads(path.read_text())
    _log(f"\n=== {plan.get('title', args.topic)} — "
         f"{datetime.now():%Y-%m-%d %H:%M} ===")
    # Verify the list can actually be carried before voicing a word of it, and
    # persist any swap so a rebuild uses the corrected list.
    if not getattr(args, "no_audit", False):
        plan = builder.preflight(plan, _log,
                                 allow_swap=not plan.get("verbatim"),
                                 force=getattr(args, "force", False))
        path.write_text(json.dumps(plan, indent=2))
    manifest = builder.build(plan, _day_dir(args.topic), _log)

    weak = [i for i in manifest["authenticity"]["by_item"]
            if i["tier"] != config.TIER_EXACT]
    _log("")
    # A tutorial has no "real subject" to verify: its steps are actions, and
    # the author-supplied query IS the instruction. Reporting all seven steps
    # of a flat-tire guide as "NOT on footage of the real subject" is not a
    # warning, it is noise that trains you to ignore the one report that
    # matters on a countdown.
    if weak and plan.get("format") == "howto":
        _log(f"  {len(weak)} step(s) on author-supplied footage, as expected "
             f"for a tutorial.")
    elif weak:
        # Autonomous by design, but never quiet about it: a numbered item on
        # anything but its own footage is the one outcome worth knowing about.
        _log("  items NOT on footage of the real subject:")
        for item in weak:
            _log(f"    #{item['rank']} {item['name']}  [{item['tier']}]")
    else:
        _log("  every item is on verified footage of the real subject")
    _log(f"\nDone: {_day_dir(args.topic) / manifest['file']}")
    return 0


def cmd_make(args) -> int:
    code = cmd_plan(args)
    return code if code else cmd_build(args)


def cmd_spec(args) -> int:
    """Build straight from a hand-written spec. No model, no quota."""
    plan = specfile.load(Path(args.file))
    topic = plan["title"] or "spec"
    day = _day_dir(topic)
    day.mkdir(parents=True, exist_ok=True)
    (day / "plan.json").write_text(json.dumps(plan, indent=2))

    _log(f"\n  {plan['title']}   ({len(plan['items'])} items, from your spec)")
    _log(f"\n  HOOK: \"{plan['hook']}\"")
    for item in plan["items"]:
        _log(f"   {item['rank']:>2}. {item['name']:<30} -> \"{item['caption']}\"")
        _log(f"       {item['line']}")
    _log(f"  CTA:   \"{plan['cta']}\"")
    spoken = sum(len(i["line"].split()) for i in plan["items"])
    spoken += len(plan["hook"].split()) + len(plan["cta"].split())
    asked = sum(float(i.get("min_seconds") or 0) for i in plan["items"])
    _log(f"\n  {spoken} words, about {spoken / config.WORDS_PER_SECOND:.0f}s spoken"
         f"   (your duration_sec totals {asked:.0f}s across the items)")
    _log(f"\n  written to {day / 'plan.json'}")
    if args.plan_only:
        return 0
    args.topic = topic
    return cmd_build(args)


def cmd_remix_plan(args) -> int:
    """Picks a YouTube video, locates its key moment, and writes our own
    narration -- the same plan-then-build split as a countdown, so the result
    can be read and corrected before anything downloads or voices."""
    data = remix.plan(args.query, mode=args.mode, topic=args.topic or "",
                      refresh=args.refresh, log=_log)
    day = _remix_day_dir(args.query)
    day.mkdir(parents=True, exist_ok=True)
    (day / "remix_plan.json").write_text(json.dumps(data, indent=2))

    seg = data["segment"]
    _log(f"\n  picked: \"{data['title']}\" — {data['channel']}")
    _log(f"  why: {data['why_picked']}")
    _log(f"  segment: {seg['start_s']:.0f}s-{seg['end_s']:.0f}s "
         f"({seg['end_s'] - seg['start_s']:.0f}s) — {seg['on_screen_label']}")
    _log(f"  reason: {seg['reason']}")
    _log(f"\n  HOOK: \"{data['hook']}\"")
    for line in data["beats"]:
        _log(f"   beat: {line}")
    credit_template = config.REMIX_CREDIT_LINE_BY_LANGUAGE.get(
        config.ASR_LANGUAGE, config.REMIX_CREDIT_LINE_EN)
    _log(f"  CREDIT (spoken): \"{credit_template.format(channel=data['channel'])}\"")
    _log(f"  CTA:   \"{data['cta']}\"")
    _log(f"\n  written to {day / 'remix_plan.json'} (edit this)")
    _log(f"  then:  ./reelforge.py remix-build \"{args.query}\"")
    return 0


def cmd_remix_build(args) -> int:
    path = _remix_plan_path(args.query)
    if not path.exists():
        _log(f"X no remix plan for '{args.query}' — run `remix-plan` first")
        return 1
    plan_data = json.loads(path.read_text())
    _log(f"\n=== remix: {plan_data.get('title', args.query)} — "
         f"{datetime.now():%Y-%m-%d %H:%M} ===")
    manifest = remix.build(plan_data, _remix_day_dir(args.query), _log)
    _log(f"\n  source: {manifest['source']['video_url']} "
         f"({manifest['source']['channel']})")
    _log(f"Done: {_remix_day_dir(args.query) / manifest['file']}")
    return 0


def cmd_remix(args) -> int:
    code = cmd_remix_plan(args)
    return code if code else cmd_remix_build(args)


def cmd_probe(_args) -> int:
    from shutil import which
    _log("ReelForge environment:")
    for binary in ("ffmpeg", "ffprobe"):
        _log(f"  {binary:16} {'found' if which(binary) else 'MISSING'}")
    _log(f"  GEMINI_API_KEY   {'set' if config.GEMINI_API_KEY else 'NOT SET'}")
    _log(f"  PEXELS_API_KEY   {'set' if config.PEXELS_API_KEY else 'NOT SET'}")
    _log(f"  voice sample     {config.VOICE_SAMPLE or '(none — stock voice)'}")
    _log(f"  worker venv      {config.REEL_VENV}")
    _log(f"  cached plans     {len(list(config.PLAN_CACHE.glob('*.json')))
                               if config.PLAN_CACHE.exists() else 0}")
    _log(f"  cached assets    {len(list(config.ASSET_CACHE.glob('*')))
                               if config.ASSET_CACHE.exists() else 0}")
    try:
        report = voiceclone.probe()
        _log("  worker:")
        for line in json.dumps(report, indent=2).splitlines():
            _log(f"    {line}")
    except voiceclone.DependencyError as exc:
        _log(f"  worker           {exc}")
        return 2
    except voiceclone.VoiceError as exc:
        _log(f"  worker           probe failed: {exc}")
        return 2
    return 0


def cmd_prune(args) -> int:
    result = builder.prune(max_mb=args.max_mb, dry_run=args.dry_run)
    cap = args.max_mb if args.max_mb is not None else config.ASSET_CACHE_MAX_MB
    verb = "would remove" if args.dry_run else "removed"
    _log(f"  {verb} {result['removed']} file(s), freed {result['freed_mb']:.0f}MB")
    _log(f"  kept {result['kept']} file(s), {result['total_mb']:.0f}MB total "
         f"(cap {cap:.0f}MB)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    subs = parser.add_subparsers(dest="command", required=True)

    topics = subs.add_parser("topics", help="suggest countdown topics")
    topics.add_argument("--count", type=int, default=8)
    topics.add_argument("--refresh", action="store_true",
                        help="spend Gemini quota instead of using the cache")
    topics.set_defaults(func=cmd_topics)

    for name, func, helptext in (("plan", cmd_plan, "write the script"),
                                 ("build", cmd_build, "render the reel"),
                                 ("make", cmd_make, "plan then build")):
        sub = subs.add_parser(name, help=helptext)
        sub.add_argument("topic")
        sub.add_argument("--count", type=int, default=config.ITEM_COUNT)
        sub.add_argument("--items", help="file of your own items, one per line")
        sub.add_argument("--refresh", action="store_true",
                         help="spend Gemini quota instead of using the cache")
        sub.add_argument("--no-audit", action="store_true",
                         help="skip the footage pre-flight")
        sub.add_argument("--force", action="store_true",
                         help="build even when most items have no footage")
        sub.set_defaults(func=func)

    spec_cmd = subs.add_parser("spec", help="build from a hand-written JSON spec")
    spec_cmd.add_argument("file")
    spec_cmd.add_argument("--plan-only", action="store_true")
    spec_cmd.add_argument("--no-audit", action="store_true")
    spec_cmd.add_argument("--force", action="store_true")
    spec_cmd.add_argument("--refresh", action="store_true",
                          help=argparse.SUPPRESS)
    spec_cmd.set_defaults(func=cmd_spec)

    for name, func, helptext in (
            ("remix-plan", cmd_remix_plan, "pick a YouTube clip and write its script"),
            ("remix-build", cmd_remix_build, "render the remix plan"),
            ("remix", cmd_remix, "remix-plan then remix-build")):
        sub = subs.add_parser(name, help=helptext)
        sub.add_argument("query", help="search keyword for the YouTube clip")
        sub.add_argument("--mode", choices=("skill", "topic"), default="skill",
                         help="skill = standalone; topic = pairs with --topic")
        sub.add_argument("--topic", default="",
                         help="today's countdown topic, when --mode topic")
        sub.add_argument("--refresh", action="store_true",
                         help="spend Gemini quota instead of using the cache")
        sub.set_defaults(func=func)

    probe = subs.add_parser("probe", help="environment check")
    probe.set_defaults(func=cmd_probe)

    prune = subs.add_parser("prune", help="evict oldest cached assets down to the size cap")
    prune.add_argument("--max-mb", type=float, default=None,
                       help=f"override the cap (default {config.ASSET_CACHE_MAX_MB}MB)")
    prune.add_argument("--dry-run", action="store_true",
                       help="report what would be removed, delete nothing")
    prune.set_defaults(func=cmd_prune)

    args = parser.parse_args()
    try:
        return args.func(args)
    except planner.QuotaExhausted as exc:
        _log(f"X {exc}")
        return 5
    except stock.StockQuotaError as exc:
        _log(f"X {exc} — try again later")
        return 5
    except voiceclone.DependencyError as exc:
        _log(f"X dependency missing: {exc}")
        return 2
    except specfile.SpecError as exc:
        _log(f"X bad spec: {exc}")
        return 1
    except builder.FootageHostile as exc:
        _log(f"\nX stopping before the expensive part:\n  {exc}")
        return 1
    except youtube.YouTubeError as exc:
        _log(f"X {exc}")
        return 2
    except (planner.PlannerError, builder.BuildError, remix.RemixError) as exc:
        _log(f"X {exc}")
        return 2
    except KeyboardInterrupt:
        _log("\ninterrupted")
        return 1
    except Exception as exc:  # noqa: BLE001
        _log(f"X Unexpected failure: {type(exc).__name__}: {exc}")
        _log(traceback.format_exc()[-900:])
        return 3


if __name__ == "__main__":
    sys.exit(main())
