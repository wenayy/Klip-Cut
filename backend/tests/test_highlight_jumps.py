from app.pipeline import highlight


def _words(n=100):
    return [{"start": i * 0.4, "end": i * 0.4 + 0.3, "word": "a."} if i % 9 == 8
            else {"start": i * 0.4, "end": i * 0.4 + 0.3, "word": "a"} for i in range(n)]


def test_jump_beats_alternate_and_never_hold_long():
    spans = highlight.jump_beats(_words(), 40.0)
    assert spans
    for a, b in spans:
        assert 0 < a < b <= 40.0
        assert b - a <= highlight.JUMP_MAX_GAP + 2.0


def test_jump_beats_without_words_still_cut():
    assert highlight.jump_beats([], 40.0)


def test_motion_includes_jump_terms():
    f = highlight._motion({"motion": 1, "duration": 20, "jumps": [(3.0, 7.0)]}, 640, 360)
    assert "between(t,3.000,7.000)" in f
