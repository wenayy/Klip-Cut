"""Panels of three or more: speaker singles, held shots, no duplicated faces."""
import os
import subprocess

from app.pipeline import content, reframe


def _tile(cx, width):
    return {"win": (width, 720), "keys": [(0.0, cx, 360.0), (5.0, cx, 360.0)]}


def test_overlapping_crops_are_a_clash():
    assert reframe.tiles_clash([_tile(600, 405), _tile(800, 405)])      # 50% overlap
    assert not reframe.tiles_clash([_tile(300, 405), _tile(900, 405)])


def _track(x, n=20):
    return {"points": [(t * 0.5, x, 0.4, 0.08, 0.2, 0.9) for t in range(n)]}


def _samples(xs, n=20):
    return [(t * 0.5, [(x, 0.4, 0.08, 0.2, 0.9) for x in xs]) for t in range(n)]


def test_panel_cuts_to_speakers_and_holds_through_short_pauses():
    tracks = [_track(0.2), _track(0.5), _track(0.8)]
    spans = [(0.0, 3.0, 0.2), (4.0, 9.5, 0.8)]        # 1s pause between them
    scenes = reframe._group_variants(tracks, _samples([0.2, 0.5, 0.8]), spans,
                                     0.0, 10.0, 1280, 720, 1080, 1920)
    assert [s["mode"] for s in scenes] == ["single", "single"]
    assert scenes[0]["end"] == scenes[1]["start"]       # held, no flash cut
    assert scenes[-1]["end"] == 10.0
    assert all(len(s["tiles"]) == 1 for s in scenes)


def test_panel_never_tiles_three_people():
    scene = reframe._scene(_samples([0.2, 0.5, 0.8]), 3, 0.0, 10.0,
                           1280, 720, 1080, 1920)
    scenes = scene if isinstance(scene, list) else [scene]
    assert all(s["mode"] in ("single", "wide") for s in scenes)


def test_small_faces_at_a_table_are_shown_whole():
    samples = [(t * 0.5, [(x, 0.4, 0.04, 0.07, 0.9) for x in (0.2, 0.5, 0.8)])
               for t in range(20)]
    scene = reframe._featured_scene(samples, 0.0, 10.0, 1280, 720, 1080, 1920)
    assert scene["mode"] == "wide" and scene["tiles"] == []


def test_layout_runs_split_at_source_cuts_and_flicker_is_absorbed():
    samples = [(t * 0.5, []) for t in range(20)]          # 0..9.5s
    runs = [(0, 6, 3), (6, 8, 4), (8, 20, 3)]               # 1s blip of four faces
    assert reframe._merge_short_runs(runs, samples, []) == [(0, 20, 3)]
    split = reframe._split_runs_at_cuts([(0, 20, 3)], samples, [5.2])
    assert split == [(0, 11, 3), (11, 20, 3)]
    # A short run on the far side of a real cut is a shot of its own: kept.
    kept = reframe._merge_short_runs([(0, 16, 3), (16, 18, 1)], samples, [7.8])
    assert kept == [(0, 16, 3), (16, 18, 1)]


def test_scene_cuts_detects_a_hard_cut(tmp_path):
    clip = str(tmp_path / "cut.mp4")
    subprocess.run(["ffmpeg", "-v", "error", "-y",
                    "-f", "lavfi", "-i", "color=red:s=160x90:d=2",
                    "-f", "lavfi", "-i", "color=blue:s=160x90:d=2",
                    "-filter_complex", "[0][1]concat=n=2:v=1", clip], check=True)
    cuts = content.scene_cuts(clip, 0.0, 4.0)
    assert len(cuts) == 1 and abs(cuts[0] - 2.0) < 0.1
