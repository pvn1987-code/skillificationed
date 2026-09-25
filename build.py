"""Turns a countdown plan into a finished 9:16 reel.

Beat order is the format, and the format is the research:

    hook          pays off inside ~2s, no preamble at all
    item N..1     counting DOWN, each on its own authentic footage
    tease         one fresh open loop around the 55% mark
    item ...      the rest of the countdown
    number 1      the payoff the whole video has been withholding
    CTA           asks for a SEND, the top-weighted Reels signal in 2026

The number and the name of each item are spoken by the pipeline, not written
by the model, so the numeral burned on screen and the numeral in the voice can
never disagree.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import config
import narrate
import planner
import videocomposite
import visuals


class BuildError(RuntimeError):
    pass


class FootageHostile(BuildError):
    """Most of the list has no authentic footage, so the TOPIC is wrong.

    Raised rather than handled, deliberately. An earlier version logged the
    warning and built anyway: "Top 10 richest counties" scored 10/10 EXACT and
    was ten courthouses, because a county's only widely photographed object is
    its courthouse. The pipeline knew in the first few seconds and spent six
    minutes and a few hundred megabytes proving it. If the analysis says a
    topic will not work, stop and say so.
    """


def _spoken_number(rank: int) -> str:
    """Chatterbox reads a bare digit unevenly, so numbers are spelled out."""
    words = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six",
             7: "seven", 8: "eight", 9: "nine", 10: "ten", 11: "eleven",
             12: "twelve", 13: "thirteen", 14: "fourteen", 15: "fifteen"}
    return words.get(rank, str(rank))


def make_beats(plan: dict) -> list:
    """The full beat list, with the mid-roll tease placed by position.

    A beat is {role, line, rank, caption}. Roles drive both the visuals ladder
    and the on-screen furniture.
    """
    items = plan.get("items") or []
    # A supplied spec writes its own voiceover, numbers and all, so the
    # pipeline must not prepend them again. A generated plan does not, and
    # relies on the pipeline speaking the number so the numeral on screen and
    # the numeral in the voice cannot disagree.
    verbatim = bool(plan.get("verbatim"))
    beats = [{"role": "hook", "line": plan.get("hook", ""), "rank": 0,
              "caption": "", "query": plan.get("hook_query", "")}]

    tease = (plan.get("tease") or "").strip()
    # Placed by fraction through the list rather than at a fixed index, so the
    # tease still lands mid-reel for a top 5 and a top 10 alike.
    tease_after = int(len(items) * config.MIDROLL_TEASE_AT) if tease else -1

    for position, item in enumerate(items):
        rank = int(item["rank"])
        # The number and name are spoken here, never by the model: this is what
        # keeps the burned-in numeral and the voice in lockstep.
        line = (item["line"] if verbatim else
                f"Number {_spoken_number(rank)}. {item['name']}. {item['line']}")
        beats.append({"role": "item", "line": line, "rank": rank,
                      "caption": item.get("caption") or item["name"],
                      "name": item["name"], "kind": item.get("kind", "other"),
                      "query": item.get("query", ""),
                      "depictable": bool(item.get("depictable")),
                      "pinned": item.get("pinned") or [],
                      "youtube_ids": item.get("youtube_ids") or [],
                      "stills": bool(item.get("stills")),
                      "min_seconds": float(item.get("min_seconds") or 0)})
        if position + 1 == tease_after:
            beats.append({"role": "tease", "line": tease, "rank": 0,
                          "caption": ""})

    cta = plan.get("cta") or config.CTA_LINE
    if cta:
        beats.append({"role": "cta", "line": cta, "rank": 0, "caption": "",
                      "query": plan.get("cta_query", "")})
    return beats


def gather_visuals(beats: list, log, day_dir: Path) -> tuple:
    """Footage for every beat. Returns (clips, per-beat clip indices, report).

    `seen` spans the whole reel so no two beats share a shot -- the one thing
    that makes a countdown look like it ran out of material.
    """
    clips, beat_clips, report = [], [], []
    seen = set()
    index_of = {}

    def register(path) -> int:
        key = str(path)
        if key not in index_of:
            index_of[key] = len(clips)
            clips.append(path)
        return index_of[key]

    for beat in beats:
        if beat["role"] == "item":
            log(f"    #{beat['rank']:<2} {beat['name']}")
            found = visuals.for_item(beat["name"], beat["kind"], seen, log,
                                     day_dir, query=beat.get("query", ""),
                                     depictable=beat.get("depictable", False),
                                     pinned=beat.get("pinned") or None,
                                     stills=beat.get("stills", False),
                                     youtube_ids=beat.get("youtube_ids") or None)
            shots = found.shots
            report.append({"rank": beat["rank"], "name": beat["name"],
                           "tier": found.tier,
                           "entity": getattr(found.entity, "qid", ""),
                           "shots": [{"file": Path(s.path).name,
                                      "subject": s.subject, "tier": s.tier,
                                      "note": s.note, **s.credit}
                                     for s in shots]})
            if found.tier != config.TIER_EXACT:
                log(f"      -> {found.tier}: on screen is "
                    f"{shots[0].subject if shots else 'nothing'}")
        else:
            # A hook or CTA with its own query is an instruction like any
            # other. Before this, every non-item beat took generic mood
            # footage and the author's query was ignored -- which is how a
            # flat-tire reel opened on aerial clouds.
            #
            # `tier` used to be hardcoded GENERIC regardless of whether
            # _authored_shots actually supplied the shot, which under-reported
            # an ILLUSTRATIVE hook/CTA as GENERIC in the manifest.
            query = beat.get("query", "")
            shots, tier = [], config.TIER_GENERIC
            if query:
                shots = visuals._authored_shots(query, beat["role"], 1, seen, log)
                if shots:
                    tier = config.TIER_ILLUSTRATIVE
                else:
                    log(f"      (nothing matched '{query}' for the "
                        f"{beat['role']}; using mood footage)")
            shots = shots or visuals.generic(1, seen, log)
            report.append({"rank": 0, "name": beat["role"],
                           "tier": tier,
                           "shots": [{"file": Path(s.path).name,
                                      "subject": s.subject, "tier": s.tier,
                                      "note": s.note, **s.credit}
                                     for s in shots]})
        if not shots:
            # A beat with no footage at all would silently inherit the previous
            # shot; better to say so and carry the previous clip explicitly.
            log(f"      ! no footage for the {beat['role']} beat")
            beat_clips.append(beat_clips[-1] if beat_clips else [])
            continue
        beat_clips.append([register(s.path) for s in shots])

    if not clips:
        raise BuildError("no footage could be sourced for any beat")
    return clips, beat_clips, report


def credit_lines(report: list) -> list:
    """One line per asset that legally requires attribution."""
    lines, seen = [], set()
    for entry in report:
        for shot in entry["shots"]:
            if not shot.get("attribution_required"):
                continue
            author = (shot.get("author") or "unknown").strip()
            licence = shot.get("licence", "")
            line = f"{shot.get('subject', '')} — {author} / {licence}"
            if line not in seen:
                seen.add(line)
                lines.append(line)
    return lines


def preflight(plan: dict, log, allow_swap: bool = True,
              force: bool = False) -> dict:
    """Check every item can be carried BEFORE anything expensive happens.

    Searches and Commons metadata are cheap; speech and Ken Burns renders are
    not. An item with no authentic footage used to be discovered only after a
    six-minute build had already committed to it.

    Items that cannot reach EXACT are offered to the planner for replacement --
    but only when the failures look item-specific. If most of the list fails,
    the TOPIC is footage-hostile, not the items, and swapping them would burn
    quota rediscovering that. "Top 10 richest counties" is the honest example:
    nobody films a county, so every entry leans on its county seat.
    """
    items = plan.get("items") or []
    if not items:
        return plan
    log("\n  checking footage before building:")
    seen = set()
    reports = []
    for item in items:
        report = visuals.audit_item(item["name"], item.get("kind", ""), seen,
                                    query=item.get("query", ""),
                                    depictable=bool(item.get("depictable")))
        reports.append(report)
        mark = {config.TIER_EXACT: "ok  ", config.TIER_PROXY: "PROXY",
                config.TIER_ILLUSTRATIVE: "yours",
                config.TIER_GENERIC: "NONE"}[report["tier"]]
        detail = report["detail"] or report["via"]
        log(f"    {mark} #{item['rank']:<2} {item['name'][:30]:32} "
            f"{report['via'][:18]:20}{detail[:36]}")

    plan["footage_audit"] = reports
    failures = [r["name"] for r in reports if r["tier"] == config.TIER_GENERIC]
    authored = [r["name"] for r in reports
                if r["tier"] == config.TIER_ILLUSTRATIVE]
    if authored:
        log(f"    {len(authored)} item(s) use the shot you specified, recorded "
            "as ILLUSTRATIVE not as the subject")
    proxies = [r["name"] for r in reports if r["tier"] == config.TIER_PROXY]
    if proxies:
        log(f"    {len(proxies)} item(s) will use a real related subject: "
            f"{', '.join(proxies[:4])}")
    if not failures:
        log("    every item has authentic footage")
        return plan

    if len(failures) > max(len(items) // 2, 1) and not force:
        raise FootageHostile(
            f"{len(failures)} of {len(items)} items have no authentic footage: "
            f"{', '.join(failures[:5])}.\n"
            "  The topic is the problem, not the items — swapping them would "
            "hit the same wall.\n"
            "  Options: pick a topic whose items are things people photograph "
            "(check `./reelforge.py topics`),\n"
            "  supply your own shots with a spec's pexels_query, or rerun with "
            "--force to build it anyway.")
    if not allow_swap:
        log(f"    ! {len(failures)} item(s) have no footage, swapping disabled")
        return plan

    log(f"    swapping {len(failures)} unfootageable item(s): {', '.join(failures)}")
    keep = [i["name"] for i in items if i["name"] not in failures]
    try:
        swaps = planner.swap_items(plan.get("topic", ""), failures, keep,
                                   len(items))
    except planner.PlannerError as exc:
        log(f"    ! could not swap ({exc}) — keeping the originals")
        return plan

    for item in items:
        replacement = swaps.get(item["name"])
        if not replacement:
            continue
        new_name = replacement.get("place_name") or replacement.get("name", "")
        check = visuals.audit_item(new_name, replacement.get("kind", ""),
                                   query=replacement.get("pexels_query", ""),
                                   depictable=bool(replacement.get("depictable")))
        if check["tier"] == config.TIER_GENERIC:
            log(f"    ! '{new_name}' has no footage either — "
                f"keeping '{item['name']}'")
            continue
        log(f"    #{item['rank']} '{item['name']}' -> '{new_name}' "
            f"({check['tier']} via {check['via']})")
        item.update(name=new_name, kind=replacement.get("kind", "other"),
                    line=replacement.get("voiceover") or item["line"],
                    caption=replacement.get("on_screen_text") or new_name,
                    query=replacement.get("pexels_query", ""),
                    depictable=bool(replacement.get("depictable")))
    return plan


def build(plan: dict, day_dir: Path, log, tag: str = "reel") -> dict:
    day_dir.mkdir(parents=True, exist_ok=True)
    # A beat with nothing to say would desync the spans from the beats, since
    # narration only returns a span for a beat it actually voiced.
    beats = [b for b in make_beats(plan) if b["line"].strip()]
    script = " ".join(b["line"] for b in beats)
    words = len(script.split())
    estimate = words / 2.8
    log(f"\n  {len(beats)} beats, {words} words (~{estimate:.0f}s spoken)")
    if not config.TARGET_SECONDS_MIN <= estimate <= config.TARGET_SECONDS_MAX:
        log(f"  ! estimated {estimate:.0f}s is outside the "
            f"{config.TARGET_SECONDS_MIN:.0f}-{config.TARGET_SECONDS_MAX:.0f}s "
            f"band that measures best")

    log("\n  sourcing footage:")
    clips, beat_clips, report = gather_visuals(beats, log, day_dir)
    exact = sum(1 for r in report if r["rank"] and r["tier"] == config.TIER_EXACT)
    total_items = sum(1 for r in report if r["rank"])
    log(f"\n  {exact}/{total_items} items on verified footage of the real subject")

    # One beat at a time. A single long utterance made Chatterbox repeat an
    # entire item and mangle the last one; see narrate.py. It also means the
    # spans below are MEASURED from the audio that ships rather than inferred
    # from word timings, so nothing can drift.
    log("\n  voicing (one beat at a time):")
    voice = narrate.narrate(beats, day_dir, log, tag)
    audio, duration = voice["audio"], voice["duration"]
    spans, timed = voice["spans"], voice["words"]
    log(f"    {duration:.1f}s total, {len(timed)} words timed")

    overlay_items = [(start, end, beat["rank"], beat["caption"])
                     for beat, (start, end) in zip(beats, spans)
                     if beat["role"] == "item"]
    credits = credit_lines(report) if config.CREDITS_CARD else []

    pages = videocomposite.plan_captions(timed, duration, fallback_text=script,
                                         beat_spans=spans)
    videocomposite.write_srt(pages, day_dir / f"{tag}.srt")
    frames_dir = day_dir / f"{tag}_frames"
    # "STEP 3" for a tutorial, "NO. 3" for a countdown.
    rank_label = (config.STEP_LABEL if plan.get("format") == "howto"
                  else config.RANK_LABEL)
    _, frame_count = videocomposite.render_caption_track(
        pages, duration, frames_dir, items=overlay_items, credits=credits,
        rank_label=rank_label)
    log(f"    {len(pages)} caption pages, {frame_count} frames, "
        f"{len(credits)} credit line(s)")

    durations = [videocomposite.probe_duration(c) for c in clips]
    segments = videocomposite.build_segments_multi(
        spans, beat_clips, durations, duration)
    cuts_per_item = len(segments) / max(total_items, 1)
    log(f"    {len(segments)} shots across {len(clips)} clips "
        f"({cuts_per_item:.1f} per item)")

    out = videocomposite.assemble(clips, audio, segments, frames_dir,
                                  day_dir / f"{tag}.mp4", duration)
    log(f"    -> {out.name} ({out.stat().st_size / 1_048_576:.1f} MB, "
        f"{duration + 0.35:.1f}s)")

    manifest = {
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "topic": plan.get("topic", ""),
        "title": plan.get("title", ""),
        "hook": plan.get("hook", ""),
        "script": script,
        "duration": round(duration + 0.35, 2),
        "words": words,
        "shots": len(segments),
        "takes": voice["takes"],
        "authenticity": {"exact": exact, "items": total_items,
                         "by_item": [{"rank": r["rank"], "name": r["name"],
                                      "tier": r["tier"]}
                                     for r in report if r["rank"]]},
        "footage": report,
        "credits": credits,
        "sources_note": plan.get("sources_note", ""),
        "warnings": plan.get("warnings", []),
        "file": out.name,
    }
    (day_dir / f"{tag}.json").write_text(json.dumps(manifest, indent=2, default=str))
    # The disclosure lives in the caption rather than burned into the frame:
    # the platform flag is what satisfies the requirement, and a label over the
    # first two seconds competes with the hook. Credits for CC BY / BY-SA
    # footage go here too when the closing card is switched off.
    caption = plan.get("caption", "")
    parts = [caption] if caption else []
    if config.DISCLOSURE_LINE:
        parts.append(config.DISCLOSURE_LINE)
    if credits and not config.CREDITS_CARD:
        parts.append("Footage: " + "; ".join(credits[:6]))
    if parts:
        (day_dir / f"{tag}.caption.txt").write_text("\n\n".join(parts))

    # The tile is the click decision, so it is part of a build rather than a
    # step someone has to remember. Never fatal: the reel is finished and
    # written by this point, and a missing thumbnail is a thing to regenerate,
    # not a reason to lose the render.
    try:
        import thumbnail
        thumbnail.build(day_dir.name)
    except Exception as exc:                      # noqa: BLE001 - see above
        log(f"  ! thumbnail failed ({type(exc).__name__}: {exc}) — "
             f"run ./thumbnail.py {day_dir.name} to retry")
    return manifest
