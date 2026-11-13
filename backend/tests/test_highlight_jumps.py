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


def _part(score, pip=None, words=None):
    return {"kind": "segment", "score": score, "pip": pip, "duration": 20.0,
            "path": "x.mp4", "words": _words(40) if words is None else words}


def test_montage_ends_on_the_best_beat_and_skips_cam_parts(monkeypatch):
    monkeypatch.setattr(highlight.content, "audio_energy", lambda p: [])
    parts = [_part(5), _part(9), _part(7), _part(8, pip={"x": 1}), _part(6)]
    beats = highlight.montage_beats(parts, n=3)
    scores = [b[0]["score"] for b in beats]
    assert scores[-1] == 9 and 8 not in scores and len(scores) == 3


def test_plan_edit_keeps_cam_parts_steady_and_talk_gets_jumps(monkeypatch):
    monkeypatch.setattr(highlight.content, "audio_energy", lambda p: [])
    parts = [_part(5), _part(6, pip={"x": 1}), _part(4, words=[])]
    highlight.plan_edit(parts, "podcast")
    assert parts[0]["jumps"] and "jumps" not in parts[1] and "jumps" not in parts[2]


def test_no_meme_cards_for_podcasts():
    parts = [{"kind": "segment", "duration": 20.0, "score": s} for s in range(1, 9)]
    plan = highlight.timeline(parts, "podcast", memes=True)
    assert not any(p["kind"] == "meme" for p in plan)
