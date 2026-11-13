from app.pipeline import analyzer


def _candidate(index, score, intent_match=False):
    return {
        "start_index": index,
        "end_index": index,
        "title": f"Moment {index}",
        "hook": "",
        "intent_match": intent_match,
        "scores": {},
        "score": score,
    }


def test_intent_keeps_one_match_and_one_stronger_general_moment():
    requested = _candidate(0, 9.0, True)
    weaker_requested = _candidate(1, 8.4, True)
    general = _candidate(2, 8.8)

    result = analyzer._balance_intent_selection(
        [requested, weaker_requested],
        [requested, general, weaker_requested],
        n_clips=2,
        intent="when they explain pricing",
    )

    assert requested in result
    assert general in result
    assert sum(bool(item["intent_match"]) for item in result) == 1


def test_intent_adds_a_strong_match_when_comparison_missed_it():
    requested = _candidate(0, 8.7, True)
    strongest = _candidate(1, 9.3)
    runner_up = _candidate(2, 9.0)

    result = analyzer._balance_intent_selection(
        [strongest, runner_up],
        [strongest, runner_up, requested],
        n_clips=2,
        intent="the launch announcement",
    )

    assert strongest in result
    assert requested in result


def test_pick_clips_returns_highest_score_first(monkeypatch):
    utterances = [
        {"start": i * 30.0, "end": i * 30.0 + 20.0, "transcript": f"Line {i}", "words": []}
        for i in range(3)
    ]
    candidates = [
        _candidate(0, 6.2),
        _candidate(1, 9.4),
        _candidate(2, 8.1),
    ]
    monkeypatch.setattr(analyzer, "SELECTION_PASSES", 1)
    monkeypatch.setattr(analyzer, "_collect_candidates", lambda *args, **kwargs: candidates)

    result = analyzer.pick_clips(utterances, n_clips=3)

    assert [item["score"] for item in result] == [9.4, 8.1, 6.2]



def test_every_genre_weighs_the_same_six_axes_to_one():
    for name, profile in analyzer.GENRES.items():
        assert set(profile["weights"]) == set(analyzer.WEIGHTS), name
        assert abs(sum(profile["weights"].values()) - 1.0) < 1e-9, name


def test_unknown_genre_falls_back_to_the_general_rubric():
    assert analyzer.genre_profile("") is analyzer.GENRES["general"]
    assert analyzer.genre_profile("cooking") is analyzer.GENRES["general"]
    assert analyzer.genre_profile("Gaming") is analyzer.GENRES["gaming"]


def test_genre_changes_the_score_not_the_contract(monkeypatch):
    reply = {"candidates": [{
        "start_index": 0, "end_index": 1, "title": "t", "hook": "h",
        "scores": {"hook": 6, "standalone": 2, "emotion": 10,
                   "ending": 6, "payoff": 6, "share": 6},
    }]}
    prompts = []

    def fake_chat(prompt, **kw):
        prompts.append(prompt)
        return reply

    monkeypatch.setattr(analyzer, "_openai_chat", fake_chat)
    utterances = [{"start": i * 5.0, "end": i * 5.0 + 4, "transcript": f"line {i}",
                   "words": []} for i in range(3)]
    general = analyzer._collect_candidates(utterances)[0]
    gaming = analyzer._collect_candidates(utterances, genre="gaming")[0]
    # A loud moment nobody can follow is a weak general clip and a strong
    # gameplay one; the range it points at is the same either way.
    assert gaming["score"] > general["score"]
    assert (gaming["start_index"], gaming["end_index"]) == (0, 1)
    assert "GAMEPLAY WITH COMMENTARY" in prompts[1]
    assert "GAMEPLAY WITH COMMENTARY" not in prompts[0]
