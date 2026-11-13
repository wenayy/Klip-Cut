"""Captions off: the clip's keyword phrases still pop up, with emoji."""

from app.pipeline import caption_emoji, subtitles


def _words(text, start=0.0, step=0.3):
    return [{"word": w, "punctuated_word": w, "start": start + i * step,
             "end": start + i * step + 0.25} for i, w in enumerate(text.split())]


def test_only_keyword_phrases_are_kept_each_as_its_own_cue(tmp_path):
    words = (_words("so the thing is argument jeet jaoge shadi haar jaoge okay")
             + _words("and later we talk about money again", start=20.0))
    pops = subtitles.keyword_popups(
        words, ["argument jeet jaoge", "shadi haar jaoge", "money"])
    cues = [" ".join(w["word"] for w in c) for c in subtitles.group_words(pops, 3)]
    assert cues == ["argument jeet jaoge", "shadi haar jaoge", "money"]
    # held long enough to read, but never into the next pop-up
    first_end = pops[2]["end"]
    assert first_end <= pops[3]["start"] and pops[-1]["end"] >= pops[-1]["start"] + 1.0
    out = subtitles.build_ass(pops, 0.0, str(tmp_path / "k.ass"), style="sig_headline",
                              keywords=["argument jeet jaoge", "shadi haar jaoge", "money"])
    text = open(out, encoding="utf-8").read().upper()
    assert "SHADI" in text and "THING" not in text and "LATER" not in text


def test_a_phrase_repeated_soon_after_is_shown_once():
    words = _words("money money talk money", step=1.0) + _words("money", start=30.0)
    pops = subtitles.keyword_popups(words, ["money"])
    assert [round(w["start"]) for w in pops] == [0, 30]


def test_long_phrases_split_evenly_and_numbers_count():
    words = _words("he manipulated one woman into 100 situations")
    pops = subtitles.keyword_popups(words, ["manipulated one woman into 100 situations"])
    sizes = [len(c) for c in subtitles.group_words(pops, 3)]
    assert sizes == [3, 3]


def test_popups_ask_for_dense_emoji(monkeypatch):
    seen = {}

    def fake_chat(prompt, **kw):
        seen["prompt"] = prompt
        return {"emoji": {"0": "\U0001f4b0", "1": "\U0001f494"}}

    monkeypatch.setattr(caption_emoji, "_openai_chat", fake_chat)
    monkeypatch.setattr(caption_emoji.subtitles, "emoji_font", lambda: {"path": "x"})
    pops = subtitles.keyword_popups(_words("money then breakup", step=2.0),
                                    ["money", "breakup"])
    marks = caption_emoji.plan(pops, "sig_headline", popups=True)
    assert marks == {0: "\U0001f4b0", 1: "\U0001f494"}       # both cues, not 1 in 7
    assert "Mark most of them" in seen["prompt"]
