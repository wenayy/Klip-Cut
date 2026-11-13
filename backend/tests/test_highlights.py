"""The sound-and-motion moment picker and the genre guess.

These stub the curve rather than decode media: what is under test is the
SHAPE of what gets picked -- lead-in, tail, spacing, honest shortfalls -- which
is arithmetic on a list of numbers and must not need ffmpeg to verify.
"""
from app.pipeline import content


def _curve(length, events):
    """A flat curve at 0.1 with triangular bumps at {second: height}."""
    hop = content.HIGHLIGHT_HOP
    out = [0.1] * int(length / hop)
    for second, height in events.items():
        centre = int(second / hop)
        for offset in range(-3, 4):
            i = centre + offset
            if 0 <= i < len(out):
                out[i] = max(out[i], height * (1 - abs(offset) / 4))
    return out


def _pick(monkeypatch, curve, n, **kw):
    monkeypatch.setattr(content, "highlight_curve", lambda *a, **k: curve)
    return content.highlight_moments(None, len(curve) * content.HIGHLIGHT_HOP, n, **kw)


def test_rare_events_are_not_flattened_by_normalisation():
    # Two seconds of event in two minutes: a percentile ceiling sits on the
    # background and used to pin the whole curve at 1.0.
    values = [0.02] * 236 + [0.9] * 4
    scaled = content._normalise(values)
    assert max(scaled[:236]) < 0.05
    assert min(scaled[236:]) > 0.9


def test_clip_opens_before_the_peak_and_closes_after_it(monkeypatch):
    clips = _pick(monkeypatch, _curve(120, {60: 1.0}), 1)
    assert len(clips) == 1
    clip = clips[0]
    assert clip["start"] <= 60 - content.EVENT_LEAD_IN
    assert clip["end"] >= 60 + content.EVENT_TAIL
    # More approach than aftermath: the setup is what makes it a moment.
    assert 60 - clip["start"] > clip["end"] - 60


def test_stops_when_the_footage_runs_out_of_events(monkeypatch):
    clips = _pick(monkeypatch, _curve(180, {40: 1.0, 120: 0.8}), 5)
    assert len(clips) == 2
    assert clips[0]["score"] >= clips[1]["score"]       # strongest first


def test_flat_footage_still_yields_one_clip(monkeypatch):
    assert len(_pick(monkeypatch, [0.1] * 240, 3)) == 1


def test_length_preference_bounds_the_window(monkeypatch):
    curve = _curve(300, {150: 1.0})
    for pref, (low, high) in content.EVENT_LENGTHS.items():
        clip = _pick(monkeypatch, curve, 1, length_pref=pref)[0]
        assert low - 0.01 <= clip["end"] - clip["start"] <= high + 0.01, pref
        assert clip["start"] <= 150 <= clip["end"], pref


def test_picks_keep_clear_of_spans_already_taken(monkeypatch):
    curve = _curve(180, {40: 1.0, 120: 0.8})
    clips = _pick(monkeypatch, curve, 1, avoid=[(30.0, 50.0)])
    assert len(clips) == 1
    assert clips[0]["start"] > 50


def test_picked_windows_never_overlap(monkeypatch):
    clips = _pick(monkeypatch, _curve(240, {50: 1.0, 62: 0.9, 150: 0.8}), 3)
    spans = sorted((c["start"], c["end"]) for c in clips)
    for (_, end), (start, _) in zip(spans, spans[1:]):
        assert start >= end


def test_commentary_inside_a_window_rides_along_as_captions(monkeypatch):
    words = [{"word": "no", "start": 58.0, "end": 58.3},
             {"word": "way", "start": 58.3, "end": 58.7},
             {"word": "later", "start": 110.0, "end": 110.4}]
    clip = _pick(monkeypatch, _curve(120, {60: 1.0}), 1, words=words)[0]
    assert [w["word"] for w in clip["words"]] == ["no", "way"]
    assert clip["silent"] is False and clip["highlight"] is True


def test_window_with_no_words_is_marked_silent(monkeypatch):
    clip = _pick(monkeypatch, _curve(120, {60: 1.0}), 1)[0]
    assert clip["silent"] is True and clip["words"] == []


def test_genre_from_title_and_speech_coverage():
    talk = [{"start": 0, "end": 540, "speaker": 0},
            {"start": 540, "end": 590, "speaker": 1}]
    solo = [{"start": 0, "end": 560, "speaker": 0}]
    sparse = [{"start": 0, "end": 100, "speaker": 0}]
    assert content.guess_genre([], 600, "GTA 6 Gameplay Walkthrough Part 1") == "gaming"
    assert content.guess_genre(talk, 600, "My chat with a founder") == "podcast"
    assert content.guess_genre(solo, 600, "How I built it") == "general"
    # Little talking over a long runtime is commentary, whatever the title says.
    assert content.guess_genre(sparse, 600, "untitled") == "gaming"
    # A title word alone does not make a podcast out of ten minutes of silence.
    assert content.guess_genre(sparse, 600, "podcast") == "gaming"
