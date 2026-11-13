"""Unit tests for the framing decisions that do not need a video file.

Run from `backend/`:

    python -m unittest discover -s tests

These cover the parts with real branching -- content classification, the
diarization-to-position mapping, the crop expression builder and the emoji
fallback -- because those are where a silent regression would cost a render
before anyone noticed. Anything that needs pixels to decide is verified by
rendering, which a unit test cannot do honestly.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.pipeline import caption_emoji, content, reframe, subtitles  # noqa: E402


def _faces(*boxes):
    return [(x, y, w, h, 0.9) for x, y, w, h in boxes]


class HudAndMusicTests(unittest.TestCase):
    def test_gta_profile_and_caption_clearance(self):
        from app.pipeline import hud
        self.assertEqual(hud.profile_for("GTA VI gameplay"), "gta")
        self.assertEqual(hud.profile_for("a podcast"), "generic")
        h = {"present": True, "regions": [
            {"kind": "bottom-left", "rect": (0.0, 0.7, 0.2, 0.3)}]}
        self.assertTrue(hud.contains(h, 0.1, 0.8))
        self.assertFalse(hud.contains(h, 0.8, 0.2))
        self.assertGreater(hud.caption_pos(h), 0.8)

    def test_facecam_broll_inserts_full_game_scenes(self):
        from app.pipeline import content
        info = {"src": (1280, 720), "duration": 10.0,
                "facecam": {"win": (320, 180), "centre": (1000, 600)},
                "motion": [(1, 0.5, 0.5, 0.1), (5, 0.7, 0.4, 0.9),
                           (8, 0.3, 0.6, 0.7)]}
        with_broll = content._gameplay_facecam_plan(
            info, 1080, 1920, variant=0, broll=True)
        without = content._gameplay_facecam_plan(
            info, 1080, 1920, variant=0, broll=False)
        self.assertEqual([s["mode"] for s in without["scenes"]], ["tile"])
        self.assertEqual([s["mode"] for s in with_broll["scenes"]], ["tile"])
        self.assertTrue(without.get("broll") is None)
        self.assertTrue(with_broll.get("broll"))

    def test_music_beds_are_available(self):
        from app.pipeline import music
        self.assertTrue(music.path_for("podcast"))
        self.assertTrue(music.path_for("gaming"))
        self.assertGreater(music.gain_for("podcast"), 0)


class DiversityTests(unittest.TestCase):
    def test_redundant_pick_is_swapped_for_a_different_topic(self):
        from app.pipeline import analyzer
        utterances = [
            {"start": 0, "end": 3,
             "transcript": "pricing strategy for startups is important"},
            {"start": 3, "end": 6,
             "transcript": "pricing strategy for startups is important"},
            {"start": 6, "end": 9,
             "transcript": "hiring your first engineer changes everything"},
        ]
        def clip(si, ei, score, title):
            return {"start_index": si, "end_index": ei, "score": score,
                    "title": title, "start": si * 3, "end": ei * 3 + 3,
                    "duration": (ei - si + 1) * 3, "keywords": []}
        a, b, c = clip(0, 0, 9, "A"), clip(1, 1, 8.5, "B"), clip(2, 2, 8, "C")
        chosen = analyzer._diversify(utterances, [a, b], [a, b, c], 2)
        self.assertIn(c, chosen)
        self.assertNotIn(b, chosen)
        self.assertFalse(any("_tokens" in x for x in chosen + [a, b, c]))


class HinglishTests(unittest.TestCase):
    def test_keyterms_include_title_and_stay_within_budget(self):
        from app.pipeline import hinglish
        terms = hinglish.keyterms("Hinglish podcast about tech", "explain the update")
        self.assertIn("tech", terms)
        self.assertLessEqual(len(terms), 60)
        self.assertLessEqual(sum(len(t) for t in terms), 420)
        self.assertEqual(len(terms), len(set(t.lower() for t in terms)))

    def test_retime_keeps_tokens_inside_the_utterance(self):
        from app.pipeline import hinglish
        original = [
            {"word": "यह", "start": 4.48, "end": 4.8},
            {"word": "feature", "start": 4.8, "end": 5.1},
            {"word": "दायहद", "start": 5.1, "end": 5.5},
            {"word": "high", "start": 5.5, "end": 5.9},
        ]
        words = hinglish.retime(original, "Yeh feature bahut hi", 4.48, 5.9)
        self.assertEqual([w["word"] for w in words],
                         ["Yeh", "feature", "bahut", "hi"])
        self.assertAlmostEqual(words[0]["start"], 4.48, places=2)
        self.assertLessEqual(words[-1]["end"], 5.9)
        for a, b in zip(words, words[1:]):
            self.assertLessEqual(a["start"], b["start"])
            self.assertLessEqual(a["end"], b["end"])

    def test_clean_rewrites_transcript_and_words(self):
        from app.pipeline import hinglish
        utterances = [{
            "start": 0.0, "end": 3.0,
            "transcript": "आज हम बात करेंगे about the new update.",
            "words": [
                {"word": "आज", "start": 0.0, "end": 0.4},
                {"word": "हम", "start": 0.4, "end": 0.8},
                {"word": "बात", "start": 0.8, "end": 1.2},
                {"word": "करेंगे", "start": 1.2, "end": 1.8},
                {"word": "about", "start": 1.8, "end": 2.2},
                {"word": "the", "start": 2.2, "end": 2.4},
                {"word": "new", "start": 2.4, "end": 2.7},
                {"word": "update.", "start": 2.7, "end": 3.0},
            ],
        }]

        def fake_chat(prompt, **kwargs):
            self.assertIn("Hinglish", prompt)
            return {"lines": [{"i": 0,
                               "text": "Aaj hum baat karenge about the new update."}]}

        hinglish.clean(utterances, chat=fake_chat)
        self.assertEqual(utterances[0]["transcript"],
                         "Aaj hum baat karenge about the new update.")
        self.assertEqual(utterances[0]["words"][0]["word"], "Aaj")
        self.assertEqual(len(utterances[0]["words"]), 8)

    def test_has_devanagari(self):
        from app.pipeline import hinglish
        self.assertTrue(hinglish.has_devanagari([{"transcript": "आज हम"}]))
        self.assertFalse(hinglish.has_devanagari([{"transcript": "aaj hum"}]))


class JobRequestTests(unittest.TestCase):
    def test_words_per_cue_accepts_none_from_frozen_options(self):
        # The job stores req.dict() on the row, so "use the preset" is written
        # as null. A plain `int = None` passed at submission but raised when the
        # worker rebuilt JobRequest from that row, wedging every job at 0%.
        from app.main import JobRequest
        req = JobRequest(url="upload://x.mp4", words_per_cue=None)
        self.assertIsNone(req.words_per_cue)


class ClassificationTests(unittest.TestCase):
    def test_two_people_together_is_a_podcast(self):
        samples = [
            (t / 2.0, _faces((0.25, 0.36, 0.055, 0.13),
                             (0.72, 0.37, 0.045, 0.10)))
            for t in range(30)
        ]
        kind, facecam = content.classify(samples, 0.002, [], 15.0, 1920, 1080)
        self.assertEqual(kind, "podcast")
        self.assertIsNone(facecam)

    def test_one_face_in_a_quiet_frame_is_a_talking_head(self):
        samples = [
            (t / 2.0, _faces((0.50, 0.36, 0.16, 0.28)))
            for t in range(30)
        ]
        kind, _ = content.classify(samples, 0.003, [], 15.0, 1920, 1080)
        self.assertEqual(kind, "talking_head")

    def test_no_faces_with_motion_is_gameplay(self):
        kind, _ = content.classify([], 0.08, [], 15.0, 1920, 1080)
        self.assertEqual(kind, "gameplay")

    def test_no_faces_without_motion_is_a_screencast(self):
        kind, _ = content.classify([], 0.0, [], 15.0, 1920, 1080)
        self.assertEqual(kind, "screencast")

    def test_small_stable_face_over_motion_is_a_facecam(self):
        # A 420px webcam in the bottom-right of a 1920-wide frame: the face is
        # small, fixed, and present in every sample.
        samples = [
            (t / 2.0, _faces((0.82, 0.78, 0.05, 0.09)))
            for t in range(30)
        ]
        kind, facecam = content.classify(samples, 0.08, [], 15.0, 1920, 1080)
        self.assertEqual(kind, "gameplay_facecam")
        self.assertIsNotNone(facecam)
        # The webcam window is landscape and sits around the face.
        self.assertGreater(facecam["win"][0], facecam["win"][1])


class SpeakerMappingTests(unittest.TestCase):
    def _tracks(self):
        return [
            {"points": [(0, 0.25, 0.5, 0.06, 0.12, 0.9)]},
            {"points": [(0, 0.75, 0.5, 0.06, 0.12, 0.9)]},
        ]

    def _rows(self):
        rows = []
        for i in range(100):
            t = i * 0.1
            rows.append((t, {0: 0.2 if t < 5 else 0.005,
                             1: 0.2 if t >= 5 else 0.005}))
        return rows

    def test_diarized_speakers_map_to_their_positions(self):
        words = [{"start": i * 0.5, "end": i * 0.5 + 0.4,
                  "speaker": 0 if i * 0.5 < 5 else 1, "word": "x"}
                 for i in range(20)]
        spans = reframe.speaker_spans(self._tracks(), self._rows(), words, 0, 10)
        self.assertEqual(len(spans), 2)
        self.assertAlmostEqual(spans[0][2], 0.25)
        self.assertAlmostEqual(spans[1][2], 0.75)

    def test_without_labels_the_moving_mouth_wins(self):
        spans = reframe.speaker_spans(self._tracks(), self._rows(), None, 0, 10)
        self.assertEqual(len(spans), 2)
        self.assertAlmostEqual(spans[0][2], 0.25)
        self.assertAlmostEqual(spans[1][2], 0.75)

    def test_one_sided_speech_does_not_become_a_punch(self):
        # Only the left speaker's mouth moves and every word is labelled to
        # them. Whatever the mapping decides, the result must be one span, so
        # `_punch_variants` sees a monologue rather than a conversation.
        rows = [(i * 0.1, {0: 0.2, 1: 0.005}) for i in range(100)]
        words = [{"start": i * 0.5, "end": i * 0.5 + 0.4,
                  "speaker": 0, "word": "x"} for i in range(20)]
        spans = reframe.speaker_spans(self._tracks(), rows, words, 0, 10)
        self.assertEqual([round(s[2], 2) for s in spans], [0.25])


class CropExpressionTests(unittest.TestCase):
    def test_static_keyframes_collapse_to_a_number(self):
        self.assertEqual(reframe.expr([(0, 100.0), (5, 100.0)], 0, 0), "100.0")

    def test_moving_keyframes_become_a_piecewise_ramp(self):
        value = reframe.expr([(0, 100.0), (4, 200.0)], 0, 0)
        self.assertIn("if(lt(t", value)
        self.assertIn("(t-0.00)", value)


class CaptionCadenceTests(unittest.TestCase):
    def _words(self):
        words = []
        t = 0.2
        for i in range(8):
            words.append({"word": f"W{i}", "punctuated_word": f"W{i}",
                          "start": t, "end": t + 0.18})
            t += 0.22
        return words

    def test_word_count_override_chunks_the_cues(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".ass", delete=False) as fh:
            path = fh.name
        try:
            subtitles.build_ass(self._words(), 0.0, path, 461,
                                style="pro_caps", play_res=(1080, 1920),
                                max_words=1)
            lines = [l for l in open(path) if ",Caption," in l]
            self.assertEqual(len(lines), 8)
            self.assertEqual(len(lines[0].strip().split(",,")[-1].split()), 1)
        finally:
            os.unlink(path)

    def test_one_word_cues_do_not_paint_every_word_as_active(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".ass", delete=False) as fh:
            path = fh.name
        try:
            subtitles.build_ass(self._words(), 0.0, path, 461,
                                style="pro_caps", play_res=(1080, 1920),
                                max_words=1)
            text = open(path).read()
            # The spoken-word colour must not be applied when the cue IS one
            # word; otherwise the whole caption turns highlight-coloured.
            self.assertNotIn("\\c&H00FF9B5A&", text)
        finally:
            os.unlink(path)

    def test_scene_positions_move_the_caption(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".ass", delete=False) as fh:
            path = fh.name
        try:
            subtitles.build_ass(self._words(), 0.0, path, 461,
                                style="pro_caps", play_res=(1080, 1920),
                                max_words=2,
                                positions=[{"start": 0, "end": 0.8, "pos": 0.76},
                                           {"start": 0.8, "end": 2.0, "pos": 0.48}])
            text = open(path).read()
            self.assertIn("\\pos(540,1459)", text)
            self.assertIn("\\pos(540,922)", text)
        finally:
            os.unlink(path)


class AutoZoomTests(unittest.TestCase):
    def _scene(self):
        return {"start": 0, "end": 10, "mode": "single",
                "tiles": [{"rect": (0, 0, 1, 1), "win": (1000, 1600),
                           "keys": [(0, 500, 800), (10, 500, 800)]}]}

    def test_zoom_splits_the_scene_and_shrinks_the_crop(self):
        out = reframe._zoom_scenes([self._scene()], [(3, 4)], factor=0.9)
        self.assertEqual(len(out), 3)
        self.assertEqual(out[1]["tiles"][0]["win"], (900, 1440))
        self.assertTrue(out[1].get("zoom"))

    def test_cut_times_ignore_zoom_cuts(self):
        plan = {"scenes": [
            {"start": 0, "end": 5, "mode": "single",
             "tiles": [{"rect": (0, 0, 1, 1)}]},
            {"start": 5, "end": 6, "mode": "single", "zoom": True,
             "tiles": [{"rect": (0, 0, 1, 1)}]},
        ]}
        self.assertEqual(reframe.cut_times(plan), [])


class EmojiOverlayTests(unittest.TestCase):
    def _words(self):
        words = []
        t = 0.2
        for token in "I AM SUPER NERVOUS".split():
            words.append({"word": token, "punctuated_word": token,
                          "start": t, "end": t + 0.3})
            t += 0.35
        return words

    def test_color_emoji_becomes_an_overlay_not_a_glyph(self):
        from app.pipeline import emoji_overlay
        if not emoji_overlay.asset_path("\U0001f630"):
            self.skipTest("no Twemoji asset and fetching disabled")
        inline, overlays = emoji_overlay.plan_overlays(
            {0: "\U0001f630"}, self._words(), "pro_caps", (1080, 1920),
            margin_v=461, clip_start=5.0)
        self.assertEqual(inline, {})
        self.assertEqual(len(overlays), 1)
        overlay = overlays[0]
        self.assertTrue(overlay["path"].endswith("1f630.png"))
        self.assertGreaterEqual(overlay["start"], 0.0)
        # Above the caption block and on the right half of the frame.
        self.assertGreater(overlay["x"], 540)
        self.assertLess(overlay["y"], 1920 - 461)

    def test_missing_asset_is_dropped_rather_than_drawn_blank(self):
        from app.pipeline import emoji_overlay
        old = emoji_overlay._FETCH
        emoji_overlay._FETCH = False
        try:
            # A simple codepoint we deliberately do not bundle.
            inline, overlays = emoji_overlay.plan_overlays(
                {0: "\U0001fae8"}, self._words(), "pro_caps",
                (1080, 1920), margin_v=461)
        finally:
            emoji_overlay._FETCH = old
        self.assertEqual(inline, {})
        self.assertEqual(overlays, [])


class EmojiFallbackTests(unittest.TestCase):
    def test_keywords_pick_emoji_without_repeating(self):
        face = {"file": "NotoEmoji-Bold.ttf"}
        if not caption_emoji._drawable("\U0001f3ac", face):
            self.skipTest("emoji font not installed")
        chosen = caption_emoji._local_plan(
            ["we made a million dollars", "the edit is the whole product",
             "the camera follows the voice"], 3, face)
        self.assertEqual(len(set(chosen.values())), len(chosen))

    def test_filler_gets_nothing(self):
        face = {"file": "NotoEmoji-Bold.ttf"}
        self.assertEqual(caption_emoji._local_plan(["and then he said"], 2, face), {})


if __name__ == "__main__":
    unittest.main()
