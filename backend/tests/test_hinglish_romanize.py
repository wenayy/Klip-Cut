"""Offline Devanagari -> Roman Hinglish, the free path every Hindi job takes."""
from app.pipeline import hinglish, subtitles


def test_everyday_words_use_the_spelling_creators_type():
    line = "यार ये feature बहुत ही ज़बरदस्त है। आज हम GTA 6 का gameplay देखेंगे"
    assert hinglish.romanize_text(line) == (
        "yaar ye feature bahut hi zabardast hai. aaj hum GTA 6 ka gameplay dekhenge")


def test_rules_drop_the_silent_vowel_and_double_only_short_closed_words():
    cases = {"करना": "karna", "देखता": "dekhta", "समझ": "samajh",
             "मतलब": "matlab", "सरकार": "sarkar", "बताया": "bataya",
             "ठीक": "theek", "फूल": "phool", "दूसरा": "dusra", "नाम": "naam",
             "आपका": "aapka", "मित्र": "mitra", "दोस्त": "dost",
             "ज्ञान": "gyaan", "अंदर": "andar", "सम्बन्ध": "sambandh"}
    got = {word: hinglish._rule_romanize(word) for word in cases}
    assert got == cases


def test_english_loanwords_come_back_in_english():
    assert hinglish.romanize_text("वीडियो लाइक करो और चैनल सब्सक्राइब") == \
        "video like karo aur channel subscribe"


def test_romanize_keeps_every_word_timing_exactly():
    words = [{"word": "यार", "punctuated_word": "यार,", "start": 1.0, "end": 1.3},
             {"word": "game", "punctuated_word": "game", "start": 1.3, "end": 1.7},
             {"word": "है", "punctuated_word": "है।", "start": 1.7, "end": 1.9}]
    utterances = [{"start": 1.0, "end": 1.9, "transcript": "यार, game है।",
                   "words": [dict(w) for w in words], "speaker": 0}]
    hinglish.romanize(utterances)
    u = utterances[0]
    assert u["transcript"] == "yaar, game hai."
    assert u["asr_transcript"] == "यार, game है।"
    assert [(w["start"], w["end"]) for w in u["words"]] == \
        [(w["start"], w["end"]) for w in words]
    assert not hinglish.has_devanagari(utterances)


def test_latin_only_transcripts_are_left_alone():
    utterances = [{"start": 0, "end": 1, "transcript": "hello there",
                   "words": [{"word": "hello", "start": 0, "end": 0.5}]}]
    hinglish.romanize(utterances)
    assert "asr_transcript" not in utterances[0]


def test_caption_fallback_uses_hinglish_not_raw_ascii():
    assert subtitles._romanize("ज़बरदस्त") == "zabardast"
    assert subtitles._romanize("नहीं") == "nahi"
