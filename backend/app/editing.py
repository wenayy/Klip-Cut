"""Re-cutting an already-rendered clip to new in/out points.

The whole pipeline already produces what an editor needs -- word-level timings,
sentence boundaries, the chosen template -- it was just being thrown away after
the first render. This module reuses the stored transcript so a boundary change
costs one ffmpeg pass and nothing else: no re-download of audio, no second
Deepgram bill, no re-running the LLM passes.

The one real cost is the source video. It is deleted after the first render
because it is by far the largest thing on disk, so an edit re-fetches it when
missing. That is a deliberate trade: a few minutes on the rare edit, against
hundreds of MB per job sitting around forever on the common path.
"""

import json
import os

from app.db import SessionLocal
from app.models import Clip, Job
from app import storage
from app.pipeline import (caption_emoji, censor, content, downloader,
                          emoji_overlay, music, pacing, reframe, render,
                          subtitles, transcriber, translation)

# A trim must still leave something watchable, and an extend must not run away
# with the whole source video.
MIN_EDIT_SECONDS = 3.0
MAX_EDIT_SECONDS = 600.0


def words_in_range(transcript: dict, start: float, end: float) -> list:
    """Word objects whose midpoint falls inside [start, end].

    Midpoint rather than full containment: a word straddling the boundary should
    belong to whichever side it mostly sits in, otherwise trimming by a tenth of
    a second can silently drop a word from the captions.
    """
    words = []
    for utt in transcriber.get_utterances(transcript):
        for w in utt.get("words", []):
            mid = (w["start"] + w["end"]) / 2
            if start <= mid <= end:
                words.append(w)
    return words


def ensure_source(job: Job, job_dir: str, on_progress=None) -> str:
    """A local path to the source video, fetched back if it is not on disk.

    Three places it can be, in descending order of preference:

      1. This container's disk -- free.
      2. Object storage -- a copy we already paid to fetch once.
      3. The original URL -- last resort, and increasingly unreliable: YouTube
         now often demands a sign-in, so an edit that falls through to here can
         fail on a clip the user already has.

    Step 2 is what makes editing survive a redeploy or a job being edited on a
    worker that never rendered it. Without it every such edit went straight to
    step 3 and gambled the user's work on a download."""
    path = os.path.join(job_dir, "source.mp4")
    if os.path.exists(path):
        return path

    restored = storage.ensure_local(job.id, job_dir, "source.mp4")
    if restored:
        print(f"[clipper] restored source for {job.id} from "
              f"{storage.driver.name} storage")
        return restored

    os.makedirs(job_dir, exist_ok=True)
    max_height = (job.options or {}).get("max_video_height", 1080)
    return downloader.download_video(job.url, path, on_progress=on_progress,
                                     max_height=max_height)


def words_from_lines(lines: list, transcript: dict = None) -> list:
    """Rebuilds word timings from caption lines, keeping real timings where possible.

    A line is {"start", "end", "text", "edited"}. Only a REWRITTEN line loses
    its per-word timings -- its words are different words, so the new tokens are
    spread evenly across the span. Karaoke on those lines becomes even rather
    than exact: the honest trade for letting text be freely rewritten.

    Lines the user did not touch keep the transcript's real word timings. This
    matters because editing one line used to re-spread EVERY line in the clip,
    so fixing a single typo quietly degraded the karaoke timing of the whole
    caption track.
    """
    original = []
    if transcript is not None:
        for utt in transcriber.get_utterances(transcript):
            original.extend(utt.get("words", []))

    words = []
    for line in lines:
        tokens = [t for t in (line.get("text") or "").split() if t]
        if not tokens:
            continue
        span_start, span_end = float(line["start"]), float(line["end"])

        if not line.get("edited", True) and original:
            # Untouched: take the real words that sit inside this span. Midpoint
            # containment, matching words_in_range, so a word on the boundary
            # lands on the side it mostly occupies.
            real = [w for w in original
                    if span_start <= (w["start"] + w["end"]) / 2 <= span_end]
            if real:
                words.extend(real)
                continue
            # No real words found (a re-timed span): fall through and spread.

        dt = (span_end - span_start) / len(tokens)
        for k, tok in enumerate(tokens):
            t0 = span_start + k * dt
            words.append({
                "word": tok.strip(".,!?"),
                "punctuated_word": tok,
                "start": round(t0, 3),
                "end": round(t0 + dt * 0.85, 3),
            })

    # Cues are grouped in order downstream, and a mixed track can interleave.
    words.sort(key=lambda w: w["start"])
    return words


def tightened_duration(transcript: dict, start: float, end: float,
                       caption_lines: list = None, tighten_pauses: bool = True,
                       speed: float = None,
                       max_pause: float = pacing.DEFAULT_MAX_PAUSE) -> float:
    """Experienced output duration for an edit recipe.

    `start`/`end` are source bounds. If pause tightening is enabled, the exported
    MP4 is the sum of kept speech segments, not the raw source span. The editor
    stores this value so cards and timelines do not keep advertising the dead
    air that export will remove.
    """
    duration = max(end - start, 0.0)
    if transcript and tighten_pauses:
        words = (words_from_lines(caption_lines, transcript)
                 if caption_lines else words_in_range(transcript, start, end))
        if words:
            segments, _, removed = pacing.tighten(
                words, start, end, max_pause=max_pause)
            if segments and removed >= 0.4:
                duration = sum(e - s for s, e in segments)
    return round(duration / max(speed or 1.0, 0.001), 1)


def rerender_clip(job_id: str, index: int, start: float, end: float,
                  storage_dir: str, templates: dict, gameplay_loops: list = None,
                  caption_style: str = None, caption_lines: list = None,
                  caption_font: str = None, translate_to: str = None,
                  ratio: str = None, overlay_list: list = None,
                  caption_size: int = None, caption_pos: float = None,
                  caption_anim: str = None, speed: float = None,
                  speed_pitched: bool = False, background: str = None,
                  caption_color: str = None, caption_active_color: str = None,
                  captions_on: bool = True,
                  keyword_captions: bool = None,
                  title_style: str = None, title_font: str = None,
                  title_size: int = None,
                  tighten_pauses: bool = None,
                  template_override: str = None,
                  auto_emoji: bool = None,
                  words_per_cue: int = None,
                  auto_zoom: bool = None,
                  auto_transitions: bool = None,
                  auto_sfx: bool = None,
                  music_bed: bool = None,
                  auto_broll: bool = None):
    """Re-cuts clip `index` of `job_id` to [start, end]. Returns the updated clip.

    `caption_style` overrides the template's caption look for this clip only.
    `caption_lines` replaces the caption text (see words_from_lines).
    """
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if not job:
            raise ValueError("Job not found.")
        clip = (session.query(Clip)
                .filter(Clip.job_id == job_id, Clip.index == index)
                .first())
        if not clip:
            raise ValueError("Clip not found.")
        if not job.transcript:
            raise ValueError(
                "This job has no stored transcript, so it cannot be edited. "
                "Jobs created before editing was added are affected -- re-run the video."
            )

        duration = end - start
        if duration < MIN_EDIT_SECONDS:
            raise ValueError(f"A clip must be at least {MIN_EDIT_SECONDS:.0f} seconds.")
        if duration > MAX_EDIT_SECONDS:
            raise ValueError(f"A clip cannot exceed {MAX_EDIT_SECONDS / 60:.0f} minutes.")
        if start < 0:
            raise ValueError("Start time cannot be negative.")

        options = job.options or {}
        tpl_key = template_override or options.get("template", "classic")
        tpl = templates.get(tpl_key, templates["classic"])
        ratio = ratio or options.get("ratio", "9:16")
        job_dir = os.path.join(storage_dir, job_id)

        source = ensure_source(job, job_dir)

        out_w, out_h = render.RATIOS.get(ratio, render.RATIOS["9:16"])

        if caption_lines:
            words = words_from_lines(caption_lines, job.transcript)
        else:
            words = words_in_range(job.transcript, start, end)

        # Recomputed, not remembered. Editing a clip must produce the same
        # composition it was delivered in -- and this call used to omit it
        # entirely, so every trim silently re-rendered a face-aware podcast clip
        # as the old letterbox and handed back a different video than the one
        # being edited. Recomputing rather than storing the original plan is
        # what makes a TRIM correct: the new range may cover different footage,
        # and framing keyed to the old range would be aimed at the wrong place.
        reframe_plan = None
        if tpl["frame"] in ("podcast", "auto"):
            try:
                use_zoom = (options.get("auto_zoom", False)
                            if auto_zoom is None else bool(auto_zoom))
                zoom_windows = (reframe._keyword_windows(words, clip.keywords, clip.title)
                                if use_zoom else None)
                use_broll = (options.get("auto_broll", True)
                             if auto_broll is None else bool(auto_broll))
                if tpl["frame"] == "auto":
                    reframe_plan = content.plan(
                        source, start, end, out_w, out_h, speaker_words=words,
                        zoom_windows=zoom_windows, variant=index,
                        title=clip.title,
                        genre=options.get("genre", ""), broll=use_broll,
                        source_title=options.get("source_title", ""))
                else:
                    reframe_plan = reframe.plan(
                        source, start, end, out_w, out_h, speaker_words=words,
                        zoom_windows=zoom_windows)
                if reframe_plan:
                    print(f"[clipper] edit: framing as "
                          f"{content.describe(reframe_plan)}")
            except Exception as exc:
                print(f"[clipper] edit: reframing failed ({exc}); fixed frame")

        if caption_pos is not None:
            # An explicit position from the editor wins over the automatic
            # placement -- the user dragged it there on purpose.
            margin_v = render.caption_margin_from_position(out_h, caption_pos)
            lock_margin = True
        elif tpl["frame"] == "gameplay":
            margin_v = render.split_caption_margin_v(out_h)
            lock_margin = True
        elif reframe_plan:
            margin_v = render.smart_caption_margin_v(reframe_plan, out_h)
            lock_margin = True
        else:
            # These frames put the video somewhere other than the middle, so the
            # generic margin strands captions in the wrong band. main.py has
            # always picked per frame here; this path had been left behind.
            src_w, src_h = render.probe_dimensions(source)
            place = {"podcast": render.podcast_caption_margin_v,
                     "auto": render.fit_caption_margin_v,
                     "fit": render.fit_caption_margin_v}.get(
                         tpl["frame"], render.caption_margin_v)
            margin_v = place(src_w, src_h, out_w, out_h)
            lock_margin = False
        size_px = caption_size or None

        # Re-cut the dead air, exactly as the first render did.
        #
        # This was missing entirely, and it quietly undid the thing that makes a
        # clip a clip: the delivered file had its pauses removed, but ANY edit --
        # changing a caption, adding a logo, nudging the speed -- re-rendered
        # from the source without them, handing back a longer, slacker video
        # than the one being edited. The user did not ask for the pauses back.
        #
        # Timing is why this has to happen before captions are built: `tighten`
        # returns words remapped onto the shortened timeline, and captions
        # written against the original timings would drift by the removed
        # seconds, further out with every pause.
        segments = None
        render_duration = duration
        use_tightening = (options.get("tighten_pauses", True)
                          if tighten_pauses is None else tighten_pauses)
        if use_tightening and words:
            # Loudness keeps laughs and reactions that have no words in them.
            from app.pipeline import energy
            tightened, remapped, removed = pacing.tighten(
                words, start, end,
                max_pause=pacing.max_pause_for_frame(tpl["frame"]),
                rms=energy.window_rms(source, start, end + 0.5))
            # The same floor the pipeline uses: under this, a re-cut buys
            # nothing and costs a seam.
            if tightened and removed >= 0.4:
                segments, words = tightened, remapped
                render_duration = sum(e - s for s, e in segments)

        style = (caption_style or (job.options or {}).get("caption_style")
                 or (job.options or {}).get("caption_style_override")
                 or tpl["caption_style"])
        print(f"[clipper] export caption_style={caption_style!r} → resolved={style!r}, captions_on={captions_on}, title={clip.title!r}, title_size={title_size!r}, title_style={title_style!r}")

        # Segments rebase the clip to zero, so the caption offset has to follow.
        caption_offset = 0.0 if segments else start
        # Layout-aware caption placement. A manual drag (caption_pos) wins; the
        # plan's own per-scene positions only apply when the user has not
        # overridden where the text goes.
        caption_positions = (reframe.caption_positions(reframe_plan, start, segments)
                             if reframe_plan and caption_pos is None else None)
        use_sfx = (options.get("auto_sfx", False)
                   if auto_sfx is None else bool(auto_sfx))
        whoosh = render.whoosh_path()
        sfx_events = ([{"path": whoosh, "time": t, "gain": 0.26}
                       for t in reframe.cut_times(reframe_plan, start, segments)]
                      if use_sfx and reframe_plan and whoosh else [])

        caption_words, mute_spans = (censor.apply(words, caption_offset)
                                     if options.get("auto_censor", True) and words
                                     else (words, None))

        has_title = (clip.title or "").strip() and title_style != "none"
        # Recomputed, not stored. An emoji map is keyed by cue index against the
        # CURRENT words, so storing it would put marks on the wrong lines the
        # moment a caption was edited. This also fixes the older bug where every
        # re-export silently dropped the emoji the first render had.
        # Captions off keeps the keyword pop-ups unless the clip asks for a
        # clean picture (the same rule as the first render).
        popups = (not captions_on and not translate_to and bool(caption_words)
                  and (options.get("keyword_captions", True)
                       if keyword_captions is None else bool(keyword_captions)))
        # A pop-up is a whole keyword phrase, not the look's 2-word beat.
        cue_words = words_per_cue or (subtitles.POPUP_MAX_WORDS if popups else None)
        if popups:
            caption_words = subtitles.keyword_popups(caption_words, clip.keywords)
        elif not captions_on:
            # No captions, no pop-ups: nothing to hang an emoji on either.
            caption_words = []
        use_emoji = (options.get("auto_emoji", True)
                     if auto_emoji is None else bool(auto_emoji))
        cue_emoji = (caption_emoji.plan(caption_words, style,
                                       max_words=cue_words, popups=popups)
                     if use_emoji and caption_words else {})
        # Color emoji are composited as PNGs; the ones without a bundled asset
        # stay as monochrome glyphs inside the subtitle file.
        inline_emoji, emoji_overlays = emoji_overlay.plan_overlays(
            cue_emoji, caption_words, style, (out_w, out_h), margin_v,
            caption_offset, max_words=cue_words,
            positions=caption_positions)
        subtitle_path = None
        if (captions_on or popups) and caption_words:
            subtitle_path = os.path.join(job_dir, f"clip_{index + 1}.ass")
            if translate_to:
                utt_lines = []
                for u in transcriber.get_utterances(job.transcript):
                    if u["end"] > start and u["start"] < end:
                        utt_lines.append({
                            "start": max(u["start"], start) - start,
                            "end": min(u["end"], end) - start,
                            "text": u["transcript"],
                        })
                if caption_lines:
                    utt_lines = [{"start": l["start"] - start, "end": l["end"] - start,
                                  "text": l["text"]} for l in caption_lines]
                translated = translation.translate_lines(utt_lines, translate_to)
                subtitles.build_ass_lines(translated, subtitle_path, margin_v,
                                          style=style, play_res=(out_w, out_h),
                                          title=clip.title or "", font=caption_font,
                                          size_px=size_px, animation=caption_anim,
                                          color=caption_color,
                                          active_color=caption_active_color,
                                          title_style=title_style, title_font=title_font,
                                          title_size=title_size,
                                          lock_margin=lock_margin)
            else:
                subtitles.build_ass(caption_words, caption_offset, subtitle_path, margin_v,
                                    style=style, play_res=(out_w, out_h),
                                    title=clip.title or "", keywords=clip.keywords,
                                    font=caption_font, size_px=size_px, animation=caption_anim,
                                    color=caption_color,
                                    active_color=caption_active_color,
                                    title_style=title_style, title_font=title_font,
                                    title_size=title_size,
                                    cue_emoji=inline_emoji,
                                    max_words=cue_words,
                                    positions=caption_positions,
                                    lock_margin=lock_margin)
        elif has_title:
            subtitle_path = os.path.join(job_dir, f"clip_{index + 1}.ass")
            subtitles.build_ass([], 0.0, subtitle_path, margin_v,
                                style=style, play_res=(out_w, out_h),
                                title=clip.title or "",
                                title_style=title_style, title_font=title_font,
                                title_size=title_size,
                                lock_margin=lock_margin)

        out_name = f"clip_{index + 1}.mp4"
        final_path = os.path.join(job_dir, out_name)
        # Render to a SIDE file and swap it in atomically.
        #
        # Writing straight to clip_N.mp4 overwrites a file the browser may be
        # streaming right then -- the player keeps its byte offsets from the old
        # file and reads them out of the new one, which is why a re-exported clip
        # stuttered and froze in the page while the downloaded copy was perfect.
        # os.replace is atomic on the same filesystem: a reader sees either the
        # whole old file or the whole new one, never a half-written mix.
        tmp_path = os.path.join(job_dir, f".{out_name}.tmp.mp4")
        render.render_clip(
            source, start, end, tmp_path,
            subtitle_path=subtitle_path,
            gameplay_path=(gameplay_loops[index % len(gameplay_loops)]
                           if gameplay_loops else None),
            ratio=ratio,
            frame=tpl["frame"],
            overlay_list=overlay_list,
            segments=segments,
            mute_spans=mute_spans,
            speed=speed,
            speed_pitched=speed_pitched,
            background=background,
            reframe_plan=reframe_plan,
            emoji_overlays=emoji_overlays,
            transitions=(options.get("auto_transitions", False)
                         if auto_transitions is None else bool(auto_transitions)),
            sfx_events=sfx_events,
            music_path=(music.path_for(options.get("genre", ""), clip.title,
                                        # Same track the first render used:
                                        # main numbers clips from 1.
                                        variant=index + 1)
                        if (options.get("music_bed", False)
                            if music_bed is None else bool(music_bed)) else None),
            music_gain=music.gain_for(options.get("genre", "")),
            # The watermark decision was made when the job was created and is
            # stored with it. Re-reading the plan here would silently strip the
            # mark from every clip a lapsed subscriber re-exports.
            watermark=options.get("watermark") or None,
        )
        if not os.path.exists(tmp_path) or os.path.getsize(tmp_path) < 1024:
            raise RuntimeError("Render produced no usable output file")
        import subprocess as _sp
        probe = _sp.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", tmp_path],
            capture_output=True, text=True, timeout=10)
        if probe.returncode != 0 or not probe.stdout.strip().replace(".", "").isdigit():
            os.unlink(tmp_path)
            raise RuntimeError("Render produced a corrupt file; original clip preserved")
        os.replace(tmp_path, final_path)
        # The edit is only real once it is in storage. Skipping this left the
        # new version on one container's disk while every other request -- and
        # every future container -- kept serving the version before the edit.
        storage.publish(job.id, job_dir, [out_name])

        # The source is deliberately cleaned up after rendering. Keep the
        # already-computed composition so reopening the editor previews the
        # same podcast framing that was burned into this export.
        if reframe_plan:
            cache_path = os.path.join(
                job_dir,
                f"reframe_v{reframe.PLAN_VERSION}_{index}_{start:.1f}_{end:.1f}.json",
            )
            with open(cache_path, "w") as cache_file:
                json.dump(reframe_plan, cache_file)

        # The editor proxy is windowed around the committed clip bounds. Once an
        # export changes those bounds, refresh that low-res proxy so reopening
        # the editor keeps the source/proxy offset correct.
        try:
            lo, hi = render.proxy_window(start, end, job.source_duration)
            render.make_proxy(source, os.path.join(job_dir, f"preview_{index + 1}.mp4"),
                              start=lo, end=hi)
        except Exception as exc:
            print(f"[clipper] edit: proxy refresh failed ({exc}); export kept")

        clip.start = start
        clip.end = end
        # Duration is what the viewer experiences, so a speed change changes it.
        clip.duration = round(render_duration / max(speed or 1.0, 0.001), 1)
        clip.words = words
        session.commit()
        return clip.to_dict()
