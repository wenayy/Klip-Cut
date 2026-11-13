"""The depth and cadence axes the signature caption looks are built on."""
import re

from app.pipeline import caption_presets, subtitles


def _words(text):
    return [{"word": w, "punctuated_word": w, "start": i * 0.4, "end": i * 0.4 + 0.38}
            for i, w in enumerate(text.split())]


def _build(tmp_path, style, text="that was the craziest", **kw):
    out = tmp_path / f"{style}.ass"
    subtitles.build_ass(_words(text), 0.0, str(out), style=style, **kw)
    return out.read_text(encoding="utf-8")


def _events(ass, style="Caption"):
    return [line.split(",,", 2)[-1].split(",,", 1)[-1]
            for line in ass.splitlines()
            if line.startswith("Dialogue:") and f",{style}," in line]


def test_every_preset_names_an_installed_font():
    for preset in caption_presets.PRESETS.values():
        if preset["font_id"]:
            assert subtitles.resolve_font(preset["font_id"]), preset["id"]


def test_every_preset_builds(tmp_path):
    for preset_id in caption_presets.PRESETS:
        assert "Dialogue:" in _build(tmp_path, preset_id), preset_id


def test_older_presets_keep_their_single_layer_output(tmp_path):
    ass = _build(tmp_path, "classic")
    assert "CaptionGlow" not in ass and "CaptionChip" not in ass
    assert all(line.startswith("Dialogue: 0,")
               for line in ass.splitlines() if line.startswith("Dialogue:"))
    # Default shadow colour and black stroke, exactly as before.
    style = next(l for l in ass.splitlines() if l.startswith("Style: Caption,"))
    assert ",&H00000000,&H80000000," in style


def test_preset_font_is_used_unless_overridden(tmp_path):
    assert "Style: Caption,Anton," in _build(tmp_path, "sig_mogul")
    assert "Style: Caption,Bangers," in _build(tmp_path, "sig_mogul", font="bangers")


def test_hard_shadow_and_coloured_stroke_reach_the_style(tmp_path):
    style = next(l for l in _build(tmp_path, "sig_bubble").splitlines()
                 if l.startswith("Style: Caption,"))
    preset = caption_presets.PRESETS["sig_bubble"]
    assert f",{preset['outline_color']},{preset['shadow_color']}," in style


def test_reveal_hides_words_that_have_not_been_spoken(tmp_path):
    events = _events(_build(tmp_path, "sig_wordpop", text="one two three",
                            max_words=3))
    hidden = [e.count("\\alpha&HFF&") for e in events]
    assert hidden == [2, 1, 0]            # one fewer hidden as each is spoken


def test_stack_puts_the_last_word_alone_on_its_own_line(tmp_path):
    events = _events(_build(tmp_path, "sig_mogul", text="that was the craziest",
                            max_words=4))
    last = events[-1]
    top, bottom = last.split("\\N")
    small = caption_presets.PRESETS["sig_mogul"]["stack_small"]
    assert f"\\fscx{small}" in top and "THAT" in top and "CRAZIEST" not in top
    assert "CRAZIEST" in bottom
    # The lead-in never pops up to full size when it is the spoken word.
    first_top = events[0].split("\\N")[0]
    assert all(int(n) < 100 for n in re.findall(r"\\fscx(\d+)", first_top))


def test_single_word_cue_in_stack_mode_is_one_line(tmp_path):
    assert "\\N" not in _events(_build(tmp_path, "sig_mogul", text="Wait."))[0]


def test_glow_draws_a_blurred_copy_on_the_layer_below(tmp_path):
    ass = _build(tmp_path, "sig_arcade")
    halo, text = _events(ass, "CaptionGlow"), _events(ass)
    assert halo and len(halo) == len(text)
    # The blur survives every reset, or the halo hardens into an outline.
    for line in halo:
        assert line.startswith("{\\blur")
        assert line.count("{\\r") == line.count("{\\r\\blur")
    assert any(l.startswith("Dialogue: 1,") for l in ass.splitlines())


def test_chip_layer_keeps_unspoken_words_outlined(tmp_path):
    ass = _build(tmp_path, "sig_deepdive")
    preset = caption_presets.PRESETS["sig_deepdive"]
    caption = next(l for l in ass.splitlines() if l.startswith("Style: Caption,"))
    chip = next(l for l in ass.splitlines() if l.startswith("Style: CaptionChip,"))
    # Text is stroked (BorderStyle 1); only the layer underneath is a box.
    assert f",1,{preset['text_outline']}," in caption
    assert f",3,{preset['outline']}," in chip
    assert len(_events(ass, "CaptionChip")) == len(_events(ass))


def test_keywords_are_drawn_larger_where_the_preset_asks(tmp_path):
    ass = _build(tmp_path, "sig_beast", text="that craziest play", keywords=["craziest"])
    scale = caption_presets.PRESETS["sig_beast"]["keyword_scale"]
    # Visible on the events where it is a keyword and not the spoken word.
    assert any(f"\\fscx{scale}\\fscy{scale}}}CRAZIEST" in e for e in _events(ass))


def test_web_presets_carry_what_the_editor_needs_to_preview():
    by_id = {p["id"]: p for p in caption_presets.web_presets()["presets"]}
    mogul, arcade = by_id["sig_mogul"], by_id["sig_arcade"]
    assert mogul["mode"] == "stack" and mogul["reveal"] and mogul["font"] == "anton"
    assert mogul["shadowColor"] == "#000000"
    assert arcade["glow"].startswith("#")
    assert by_id["classic"]["font"] is None and by_id["classic"]["glow"] is None


def test_phrase_keywords_match_runs_of_words():
    words = _words("so what is a woman really")
    hits = subtitles.keyword_hits(words, ["what is a woman"])
    assert hits == {1, 2, 3, 4}


def test_only_number_shaped_tokens_count_as_numbers():
    words = _words("PS5 W0 covers 40% of $5 in 1st place")
    texts = {words[i]["word"] for i in subtitles.keyword_hits(words, [])}
    assert texts == {"40%", "$5", "1st"}


def test_possessives_plurals_and_numbers_are_keywords():
    words = _words("women's happiness fell 20 percent since 2008")
    hits = subtitles.keyword_hits(words, ["women"])
    texts = {words[i]["word"] for i in hits}
    assert texts == {"women's", "20", "percent", "2008"}


def test_keyword_phrase_survives_cue_chunking(tmp_path):
    ass = _build(tmp_path, "classic", text="what is a woman", keywords=["what is a woman"])
    # Four words, max three per cue: the phrase spans two cues, all four marked.
    assert ass.count("&H000090FF&") >= 4 or "WHAT" not in ass


def test_keyword_sharing_the_highlight_colour_is_drawn_larger(tmp_path):
    ass = _build(tmp_path, "pro_caps", text="the main claim here",
                 keywords=["claim"])
    # pro_caps uses one accent for keyword and spoken word; size tells them apart.
    assert "\\fscx112\\fscy112}CLAIM" in ass


def _talk(text, keywords=None):
    words, t = [], 0.2
    for tok in text.split():
        words.append({"word": tok.lower().strip("."), "punctuated_word": tok,
                      "start": t, "end": t + 0.3})
        t += 0.4
    return words


def test_keywords_rotate_colours_and_a_keyword_keeps_its_colour():
    words = _talk("money talks and growth matters but money wins")
    hits = subtitles.keyword_hits(words, ["money", "growth"])
    slots = subtitles.keyword_slots(words, hits)
    assert slots[0] == slots[6] == 0          # "money" both times
    assert slots[3] == 1                      # "growth" is the next colour
    palette = caption_presets.keyword_palette(caption_presets.PRESETS["sig_beast"])
    assert len(set(palette)) == 3


def test_calm_and_legacy_looks_keep_one_keyword_colour():
    for pid in ("podcast_clean", "cinema_film", "classic"):
        assert len(caption_presets.keyword_palette(caption_presets.PRESETS[pid])) == 1


def test_headline_draws_a_bar_layer_under_the_big_line(tmp_path):
    out = tmp_path / "h.ass"
    subtitles.build_ass(_talk("clipping with ai"), 0.0, str(out), style="sig_headline")
    ass = out.read_text()
    assert "Style: CaptionBar," in ass
    bars = [l for l in ass.splitlines() if ",CaptionBar," in l and l.startswith("Dialogue")]
    caps = [l for l in ass.splitlines() if ",Caption," in l and l.startswith("Dialogue")]
    assert bars and len(bars) == len(caps)
    assert "\\q2" in bars[0] and "\\fsp" in bars[0] and "\\N" in bars[0]
    assert "CLIPPING" in caps[0].split("\\N")[0]          # first word on the big line


def test_backdrop_is_one_band_behind_outlined_text(tmp_path):
    out = tmp_path / "b.ass"
    subtitles.build_ass(_talk("clipping with ai"), 0.0, str(out), style="sig_backdrop")
    ass = out.read_text()
    chip = next(l for l in ass.splitlines() if l.startswith("Style: CaptionChip,"))
    assert chip.split(",")[15] == "4"                      # one box per event
    caption = next(l for l in ass.splitlines() if l.startswith("Style: Caption,"))
    assert caption.split(",")[8] == "-1"                   # italic
    assert any("\\xbord" in l for l in ass.splitlines() if ",CaptionChip," in l)


def test_bounce_drops_in_and_overshoots():
    tags = subtitles._active_override(caption_presets.PRESETS["sig_bounce"])
    assert "\\fscx60" in tags and "\\fscx118" in tags and "\\fscx93" in tags


def test_landscape_canvas_scales_caption_size():
    st = subtitles._preset_style("sig_beast")
    wide = subtitles._fit_canvas(st, (1920, 1080))
    assert wide["size"] < st["size"] * 0.8
    assert subtitles._fit_canvas(st, (1080, 1920))["size"] == st["size"]


def test_every_caption_layer_moves_with_the_scene(tmp_path):
    out = tmp_path / "g.ass"
    subtitles.build_ass(_talk("that was insane"), 0.0, str(out), style="sig_arcade",
                        positions=[{"start": 0.0, "end": 5.0, "pos": 0.6}])
    for line in out.read_text().splitlines():
        if line.startswith("Dialogue") and (",CaptionGlow," in line or ",Caption," in line):
            assert "\\pos(" in line


def test_signature_looks_are_not_oversized():
    # People cut 124px headline words to 62-82 by hand; nothing ships huge.
    from app.pipeline import caption_presets
    for p in caption_presets._LIST:
        assert p["size"] <= 112, p["id"]


def test_punchy_looks_default_to_two_words_a_caption(tmp_path):
    # Three words read as a sentence scrolling past; sentence looks keep theirs.
    assert caption_presets.PRESETS["sig_headline"]["max_words"] == 2
    assert caption_presets.PRESETS["podcast_clean"]["max_words"] > 2
    cues = _events(_build(tmp_path, "sig_beast", text="one two three four"))
    assert all(len(_re_words(c)) <= 2 for c in cues)


def _re_words(event):
    return [w for w in re.sub(r"\{[^}]*\}", "", event).replace("\\N", " ").split() if w]
