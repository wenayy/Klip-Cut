"""Podcast speaker switching: held shots, earned close-ups, no zoom churn."""

from app.pipeline import reframe


def _shots_ok(shots, start, end):
    assert shots[0][0] == start and shots[-1][1] == end
    for (a0, a1, _), (b0, b1, _) in zip(shots, shots[1:]):
        assert abs(a1 - b0) < 1e-9                  # contiguous, no overlaps
    if len(shots) > 1:
        assert all(e - s >= reframe.SHOT_MIN - 1e-9 for s, e, _ in shots)


def test_fast_back_and_forth_stays_on_the_home_shot():
    turns = [(t, t + 1.4, t // 2 % 3) for t in range(0, 20, 2)]  # 1.4 s each
    shots = reframe._speaker_cuts(turns, 0.0, 20.0)
    assert shots == [(0.0, 20.0, None)]


def test_a_short_interjection_does_not_steal_the_shot():
    turns = [(0.0, 6.0, 0), (6.2, 7.0, 1), (7.1, 14.0, 0)]
    shots = reframe._speaker_cuts(turns, 0.0, 14.0)
    assert shots == [(0.0, 14.0, 0)]


def test_cut_follows_the_new_voice_and_holds_through_a_pause():
    turns = [(0.0, 5.0, 0), (6.0, 12.0, 1)]
    shots = reframe._speaker_cuts(turns, 0.0, 12.0)
    assert [s[2] for s in shots] == [0, 1]
    assert abs(shots[1][0] - (6.0 + reframe.CUT_LAG)) < 1e-9   # J-cut, not early
    _shots_ok(shots, 0.0, 12.0)


def test_a_long_lull_goes_home_and_every_shot_is_readable():
    turns = [(0.0, 4.0, 0), (10.0, 15.0, 2), (15.5, 16.4, 1), (17.0, 22.0, 1)]
    shots = reframe._speaker_cuts(turns, 0.0, 24.0)
    assert None in [s[2] for s in shots]
    _shots_ok(shots, 0.0, 24.0)


def _tile(xs):
    return {"rect": (0, 0, 1, 1), "win": (600, 1066),
            "keys": [(i * 0.5, x, 500.0) for i, x in enumerate(xs)]}


def test_a_seated_speaker_is_locked_off():
    jitter = [600 + (15 if i % 2 else -15) for i in range(20)]
    tile = reframe._steady(_tile(jitter), 0.0, 9.5)
    assert len(tile["keys"]) == 2
    assert tile["keys"][0][1:] == tile["keys"][1][1:]


def test_a_subject_who_really_moves_is_still_tracked():
    walking = [300 + i * 40 for i in range(20)]
    tile = reframe._steady(_tile(walking), 0.0, 9.5)
    assert len(tile["keys"]) == 20


def _faces(xs, h=0.2):
    return [(x, 0.4, h * 0.6, h, 0.9) for x in xs]


def test_similar_faces_never_hop_the_home_shot_between_people():
    # Equal-sized faces whose sizes wobble: per-sample "biggest face" hopped.
    samples = [(t * 0.5, [(x, 0.4, 0.1, 0.2 + (0.01 if (t + k) % 2 else 0), 0.9)
                          for k, x in enumerate((0.2, 0.5, 0.8))])
               for t in range(20)]
    scene = reframe._featured_scene(samples, 0.0, 10.0, 1920, 1080, 1080, 1920)
    assert scene["mode"] in ("wide", "split") and not reframe.tiles_clash(scene["tiles"])


def test_one_dominant_face_is_the_home_shot_and_holds_still():
    samples = [(t * 0.5, [(0.3, 0.4, 0.2, 0.34, 0.9), (0.75, 0.4, 0.06, 0.1, 0.9)])
               for t in range(20)]
    scene = reframe._featured_scene(samples, 0.0, 10.0, 1920, 1080, 1080, 1920)
    assert scene["mode"] == "single"
    assert len(scene["tiles"][0]["keys"]) == 2


def test_face_count_flicker_inside_one_shot_is_one_composition():
    xs = (0.15, 0.4, 0.62, 0.85)
    pattern = [4] * 8 + [2] * 6 + [3] * 7 + [4] * 9      # heads turning
    samples = [(i * 0.5, _faces(xs[:n])) for i, n in enumerate(pattern)]
    runs = reframe._merge_short_runs(reframe._runs(reframe.stabilize_layouts(pattern)),
                                     samples, [])
    assert len(runs) > 1                               # the old per-run layouts
    assert reframe._whole_shots(runs, samples, []) == [(0, len(samples), 4)]
    # ...but a real camera cut still separates two shots.
    cut = samples[14][0] - 0.2
    split = reframe._split_runs_at_cuts(runs, samples, [cut])
    assert len(reframe._whole_shots(split, samples, [cut])) >= 2


def test_a_four_person_table_edit_has_no_flash_shots():
    xs = (0.15, 0.4, 0.62, 0.85)
    samples = [(i * 0.5, _faces(xs)) for i in range(80)]           # 40 s
    spans = [(0.0, 6.0, 0.15), (6.3, 7.1, 0.4), (7.4, 8.0, 0.62),  # crosstalk
             (8.2, 15.0, 0.62), (16.0, 17.5, 0.85), (18.0, 26.0, 0.85),
             (26.2, 27.0, 0.15), (27.5, 28.4, 0.4), (29.0, 40.0, 0.4)]
    scenes = reframe._scene(samples, 4, 0.0, 40.0, 1920, 1080, 1080, 1920,
                            active_spans=spans)
    scenes = scenes if isinstance(scenes, list) else [scenes]
    assert all(s["end"] - s["start"] >= reframe.SHOT_MIN - 1e-9 for s in scenes)
    assert len(scenes) <= 6                     # was a cut every couple of seconds
    for scene in scenes:
        for tile in scene["tiles"]:
            assert len(tile["keys"]) == 2       # locked-off close-ups


def _long_scene(start, end):
    return {"start": start, "end": end, "mode": "single",
            "tiles": [{"rect": (0, 0, 1, 1), "win": (1000, 1600),
                       "keys": [(start, 500, 800), (end, 500, 800)]}]}


def test_punch_ins_stay_off_short_shots_and_cuts_and_are_spaced():
    scenes = [_long_scene(0, 3), _long_scene(3, 12), _long_scene(12, 20)]
    windows = [(1.0, 2.0),      # inside a 3 s shot: no
               (3.3, 4.0),      # right on the cut at 3 s: no
               (6.0, 7.0),      # inside a long hold, clear of its cuts: yes
               (9.0, 10.0),     # 2 s after the last punch: no
               (14.0, 15.0)]    # 7 s later, clear of the cut at 12 s: yes
    out = reframe._zoom_scenes(scenes, windows)
    punches = [(s["start"], s["end"]) for s in out if s.get("zoom")]
    assert punches == [(6.0, 7.0), (14.0, 15.0)]
    assert out[0] is scenes[0]                  # the short shot is untouched
