"""The crop moves like an operator: holds stay still, moves ease in and out."""
import re

from app.pipeline import reframe


def _eval(expression, t):
    """Evaluate the ffmpeg crop expression in Python at time t."""
    py = expression.replace("\\,", ",").replace("lt(", "_lt(").replace("if(", "_if(")
    return eval(py, {"_lt": lambda a, b: a < b,
                     "_if": lambda c, a, b: a if c else b, "t": t})


def test_a_hold_then_a_move_does_not_turn_into_a_drift():
    # Still at 500 from 0s to 3s, then reframes to 600 by 4s.
    keys = [(0.0, 500), (1.0, 500), (2.0, 500), (3.0, 500), (3.5, 550), (4.0, 600),
            (5.0, 600)]
    expression = reframe.expr(keys, 0, 0)
    for t in (0.0, 1.0, 2.0, 2.99):
        assert abs(_eval(expression, t) - 500) < 1e-6, t
    assert abs(_eval(expression, 4.0) - 600) < 1e-6
    assert abs(_eval(expression, 4.8) - 600) < 1e-6


def test_one_move_across_many_samples_is_one_eased_segment():
    keys = [(0.0, 100)] + [(1 + i * 0.5, 100 + i * 20) for i in range(6)] + [(5.0, 200)]
    segments = reframe.motion_segments(keys)
    moving = [(a, b) for a, b in zip(segments, segments[1:]) if a[1] != b[1]]
    assert len(moving) == 1


def test_moves_start_and_finish_at_rest():
    keys = [(0.0, 0), (1.0, 0), (3.0, 200), (4.0, 200)]
    e = reframe.expr(keys, 0, 0)
    eps = 0.01
    start_speed = (_eval(e, 1.0 + eps) - _eval(e, 1.0)) / eps
    mid_speed = (_eval(e, 2.0 + eps) - _eval(e, 2.0)) / eps
    end_speed = (_eval(e, 3.0) - _eval(e, 3.0 - eps)) / eps
    assert start_speed < 0.1 * mid_speed and end_speed < 0.1 * mid_speed
    assert abs(_eval(e, 2.0) - 100) < 1e-6          # symmetric ease


def test_cuts_still_snap():
    # smooth() marks a cut as two keys 40ms apart; it must stay a step.
    keys = [(0.0, 100), (1.96, 100), (2.0, 900), (3.0, 900)]
    e = reframe.expr(keys, 0, 0)
    assert abs(_eval(e, 1.95) - 100) < 1e-6 and abs(_eval(e, 2.0) - 900) < 1e-6


def test_still_shot_is_a_constant():
    assert reframe.expr([(0, 400.2), (1, 400.4), (2, 400.0)], 0, 0) == "400.2"


def test_expression_stays_valid_ffmpeg_syntax():
    e = reframe.expr([(0, 0), (1, 50), (2, 50), (3, 10)], 0, 0)
    assert e.count("(") == e.count(")")
    assert not re.search(r"[^\\],", e)            # every comma escaped for ffmpeg
