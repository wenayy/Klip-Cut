from app.pipeline import pacing


def _w(a, b):
    return {"start": a, "end": b, "word": "x"}


def test_clip_rings_out_through_a_laugh_after_a_short_dip():
    rms = [1000] * 50 + [100] * 3 + [1200] * 15 + [50] * 20   # 0.1 s frames
    assert pacing.ring_out(rms, 0.0, [_w(0.1, 0.4), _w(4.5, 4.9)], 5.0) > 6.5


def test_quiet_end_does_not_ring_out():
    rms = [1000] * 50 + [50] * 30
    assert pacing.ring_out(rms, 0.0, [_w(0.1, 0.4)], 5.0) == 5.0


def test_loud_gap_between_lines_is_kept():
    words = [_w(0.0, 1.0), _w(3.0, 4.0)]
    rms = [1000] * 10 + [900] * 20 + [1000] * 10
    assert len(pacing.plan_segments(words, 0.5, rms, 0.0)) == 1
    assert len(pacing.plan_segments(words, 0.5)) == 2
