"""Highlight video: length, selection across the whole video, assembly plan."""

from app import billing
from app.pipeline import highlight


def test_auto_length_follows_the_source():
    assert highlight.target_seconds(60 * 60) == 540            # 1 h -> 9 min
    assert highlight.target_seconds(3 * 60 * 60) == highlight.MAX_TARGET
    assert highlight.target_seconds(10 * 60) == highlight.MIN_TARGET
    assert highlight.target_seconds(60 * 60, minutes=10) == 600
    assert highlight.target_seconds(12 * 60, minutes=15) == 12 * 60 * 0.6


def _moment(start, length, score):
    return {"start": float(start), "end": float(start + length), "score": score}


def test_selection_fills_the_budget_from_across_the_whole_video():
    hour = 3600
    # The best material is all in the first ten minutes; decent moments
    # elsewhere must still be represented.
    pool = [_moment(30 + i * 60, 50, 9.0) for i in range(9)]
    pool += [_moment(900 + i * 420, 45, 7.0) for i in range(6)]
    chosen = highlight.select_segments(pool, 480, hour)
    total = sum(c["end"] - c["start"] for c in chosen)
    assert 0.9 * 480 <= total <= 1.15 * 480
    assert any(c["start"] > 1800 for c in chosen)          # not only the start
    assert [c["start"] for c in chosen] == sorted(c["start"] for c in chosen)
    for a, b in zip(chosen, chosen[1:]):
        assert b["start"] >= a["end"] + highlight.SEGMENT_GAP - 1e-9


def test_selection_skips_fragments_and_overlong_runs():
    pool = [_moment(10, 4, 9.9), _moment(100, 400, 9.8), _moment(700, 60, 6.0)]
    chosen = highlight.select_segments(pool, 300, 3600)
    assert [c["start"] for c in chosen] == [700]


def _parts(n, length=40.0):
    return [{"kind": "segment", "duration": length, "score": float(i % 4),
             "title": f"m{i}", "source_start": i * 600.0,
             "source_end": i * 600.0 + length} for i in range(n)]


def test_memes_follow_big_moments_never_the_last_and_never_back_to_back():
    slots = highlight.meme_slots([5, 9, 8, 1, 7, 9, 2, 3, 9])
    assert 8 not in slots
    assert all(i + 1 not in slots for i in slots)
    assert len(slots) <= 9 // highlight.MEME_EVERY


def test_timeline_offsets_add_up_and_the_teaser_opens():
    parts = [{"kind": "teaser", "duration": 7.0}] + _parts(6)
    plan = highlight.timeline(parts, "podcast", memes=True)
    assert plan[0]["kind"] == "teaser" and plan[1]["transition"] == "fadeblack"
    length = plan[0]["duration"]
    for seg in plan[1:]:
        assert abs(seg["offset"] - (length - seg["fade"])) < 1e-6
        length += seg["duration"] - seg["fade"]
    assert abs(highlight.total_duration(plan) - length) < 1e-6
    assert plan[-1]["kind"] == "segment"


def test_tags_name_long_skips_and_chapters_point_into_the_video():
    plan = highlight.timeline([{"kind": "teaser", "duration": 7.0}] + _parts(4),
                              memes=False)
    tags = highlight.context_tags(plan)
    assert set(tags.values()) == {"9 MIN LATER"}        # 600 - 40 s between parts
    chapters = highlight.chapters(plan)
    assert [c["title"] for c in chapters] == ["m0", "m1", "m2", "m3"]
    assert chapters[0]["at"] > 0 and chapters == sorted(chapters, key=lambda c: c["at"])


def test_teaser_keeps_setup_before_its_peak():
    energy = [0.2] * 40
    energy[20] = 1.0                                       # spike at 10 s
    start, end = highlight.teaser_window([], 20.0, energy, length=6.0)
    assert start < 10.0 - 6.0 * 0.3 + 1e-9 and end > 10.0


def test_join_graph_normalises_inputs_and_borrows_silence():
    plan = highlight.timeline([{"kind": "teaser", "duration": 7.0}] + _parts(3),
                              memes=False)
    graph = highlight.join_graph(plan, {2: 9}, 1920, 1080, whoosh_input=10)
    assert graph.count("fps=30") == len(plan)
    assert "[9:a]aresample" in graph                      # silent part's audio
    assert graph.count("xfade=") == len(plan) - 1
    assert "[vout]" in graph and "[aout]" in graph


def test_highlight_is_priced_by_finished_minutes():
    assert billing.credits_for_highlight(9.2 * 60) == 10 * billing.HIGHLIGHT_CREDITS_PER_MINUTE
    with_extras = billing.credits_for_highlight(300, True, True)
    assert with_extras == (5 * (billing.HIGHLIGHT_CREDITS_PER_MINUTE
                                + billing.HIGH_QUALITY_PER_HIGHLIGHT_MINUTE)
                           + billing.ADVANCED_MODEL_PER_JOB)
    assert billing.highlight_breakdown(300)["total"] == billing.credits_for_highlight(300)


def test_every_part_has_both_tracks_pinned_to_one_length():
    # The lip-sync bug: each part's audio was a few ms off its video, and the
    # error stacked at every crossfade. Both tracks are now cut to one length.
    plan = highlight.timeline(_parts(3, length=21.37), memes=False)
    graph = highlight.join_graph(plan, {}, 1920, 1080)
    for k in range(3):
        assert f"[{k}:v]trim=0:21.370,setpts=PTS-STARTPTS" in graph
        assert "apad,atrim=0:21.370,asetpts=PTS-STARTPTS" in graph


def test_parts_move_and_punch_in_only_where_planned():
    still = {"duration": 10.0}
    assert highlight._motion(still, 1920, 1080) == ""
    moving = highlight._motion({"duration": 10.0, "motion": -1, "punch": 4.0}, 1920, 1080)
    assert "eval=frame" in moving and "crop=1920:1080" in moving and "(t-4.00)" in moving


def test_music_bed_is_ducked_under_speech():
    plan = highlight.timeline(_parts(2), memes=False)
    graph = highlight.join_graph(plan, {}, 1920, 1080, music_input=5, music_gain=0.12)
    assert "[5:a]" in graph and "sidechaincompress" in graph and "volume=0.120" in graph


def test_watch_along_cam_is_found_in_its_corner_and_enlarged():
    from app.pipeline import content
    # streamer cam pinned bottom-right; a face in the watched video moves around
    samples = [(t * 0.5, [(0.93, 0.83, 0.06, 0.12, 0.9), (0.25 + 0.01 * (t % 5), 0.35, 0.08, 0.15, 0.9)])
               for t in range(40)]
    cam = content.corner_facecam(samples, 1280, 720)
    assert cam and cam["centre"][0] > 1100 and cam["focus"][0] < 500
    pip = highlight.pip_for(cam, 1280, 720, 1920, 1080)
    assert pip["box"][0] > 960 and pip["box"][2] >= 0.29 * 1920      # big, same corner
    assert pip["zoom"][0] == 0                                         # push toward the left content
    plan = highlight.timeline([{**_parts(1)[0], "pip": pip}] + _parts(1), memes=False)
    assert "overlay=" in highlight.join_graph(plan, {}, 1920, 1080)


def test_a_single_face_is_never_a_watch_along_cam():
    from app.pipeline import content
    samples = [(t * 0.5, [(0.93, 0.83, 0.06, 0.12, 0.9)]) for t in range(40)]
    assert content.corner_facecam(samples, 1280, 720) is None


def test_landscape_captions_sit_low():
    from app.pipeline import render
    assert render.caption_margin_v(1280, 720, 1920, 1080) == int(1080 * 0.08)
    assert render.caption_margin_v(1280, 720, 1080, 1920) > 1920 * 0.2   # vertical unchanged


def test_highlight_uses_impact_riser_pop_and_a_light_grade():
    plan = highlight.timeline(_parts(2), memes=False)
    graph = highlight.join_graph(
        plan, {}, 1920, 1080, impact_input=5, impact_times=[1.0, 2.0],
        riser_input=6, pop_input=7, pop_times=[1.5])
    assert "asplit=2[imps0][imps1]" in graph
    assert "volume=0.50" in graph and "volume=0.38" in graph and "volume=0.22" in graph
    assert "[impmix]" in graph and "[rismix]" in graph and "[popmix]" in graph
    assert "eq=contrast=1.04:saturation=1.06" in graph


def test_long_skip_gets_a_full_frame_context_card():
    plan = highlight.timeline([{**_parts(1)[0], "source_start": 0, "source_end": 10},
                               {**_parts(1)[0], "source_start": 1200,
                                "source_end": 1210}], memes=False)
    import os, tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
        fh.write("20 MIN LATER")
        path = fh.name
    try:
        graph = highlight.join_graph(plan, {}, 1920, 1080, font="font.ttf",
                                     card_files={1: path})
        assert "color=black@0.78" in graph and "textfile=" in graph
    finally:
        os.unlink(path)
