from app.pipeline import reframe, render


def _face(x, y=0.35, w=0.12, h=0.28):
    return (x, y, w, h, 0.95)


def _samples(layouts, step=0.5):
    rows = []
    for i, count in enumerate(layouts):
        faces = [_face(x) for x in (0.25, 0.72, 0.48, 0.88)[:count]]
        rows.append((i * step, faces))
    return rows


def test_plan_changes_layout_per_camera_shot(monkeypatch):
    # Single close-up -> genuine two-shot -> single close-up.
    samples = _samples([1] * 5 + [2] * 6 + [1] * 5)
    monkeypatch.setattr(
        reframe, "detect_faces", lambda *args, **kwargs: (samples, (1920, 1080))
    )

    plan = reframe.plan("unused.mp4", 0.0, 8.0, 1080, 1920)

    assert plan["version"] == reframe.PLAN_VERSION
    assert plan["mode"] == "adaptive"
    assert [scene["mode"] for scene in plan["scenes"]] == [
        "single", "split", "single"
    ]
    assert [len(scene["tiles"]) for scene in plan["scenes"]] == [1, 2, 1]

    split = plan["scenes"][1]
    left = split["tiles"][0]["keys"][0][1]
    right = split["tiles"][1]["keys"][0][1]
    assert left < right


def test_one_sample_second_face_does_not_duplicate_speaker(monkeypatch):
    samples = _samples([1] * 5 + [2] + [1] * 5)
    monkeypatch.setattr(
        reframe, "detect_faces", lambda *args, **kwargs: (samples, (1920, 1080))
    )

    plan = reframe.plan("unused.mp4", 0.0, 5.5, 1080, 1920)

    assert [scene["mode"] for scene in plan["scenes"]] == ["single"]
    assert len(plan["scenes"][0]["tiles"]) == 1


def test_no_face_scene_uses_wide_fallback(monkeypatch):
    samples = _samples([1] * 5 + [0] * 5 + [1] * 5)
    monkeypatch.setattr(
        reframe, "detect_faces", lambda *args, **kwargs: (samples, (1920, 1080))
    )

    plan = reframe.plan("unused.mp4", 0.0, 7.5, 1080, 1920)

    assert [scene["mode"] for scene in plan["scenes"]] == [
        "single", "wide", "single"
    ]
    assert plan["scenes"][1]["tiles"] == []


def test_dominant_speaker_gets_larger_reaction_panel(monkeypatch):
    samples = [
        (i * 0.5, [_face(0.25, w=0.09), _face(0.72, w=0.17)])
        for i in range(6)
    ]
    words = [
        {"start": 0.0, "end": 2.8, "speaker": 0},
        {"start": 2.8, "end": 3.0, "speaker": 1},
    ]
    monkeypatch.setattr(
        reframe, "detect_faces", lambda *args, **kwargs: (samples, (1920, 1080))
    )

    plan = reframe.plan(
        "unused.mp4", 0.0, 3.0, 1080, 1920, speaker_words=words)

    scene = plan["scenes"][0]
    assert scene["mode"] == "reaction"
    assert scene["tiles"][0]["rect"][3] == 0.62
    assert scene["tiles"][1]["rect"][3] == 0.38


def test_close_pair_stays_in_one_natural_frame(monkeypatch):
    samples = [
        (i * 0.5, [_face(0.44, w=0.08), _face(0.56, w=0.08)])
        for i in range(6)
    ]
    monkeypatch.setattr(
        reframe, "detect_faces", lambda *args, **kwargs: (samples, (1920, 1080))
    )

    plan = reframe.plan("unused.mp4", 0.0, 3.0, 1080, 1920)

    assert plan["scenes"][0]["mode"] == "together"
    assert len(plan["scenes"][0]["tiles"]) == 1


def test_adaptive_filter_concatenates_scenes():
    plan = {
        "mode": "adaptive",
        "src": (1920, 1080),
        "scenes": [
            {
                "start": 0.0,
                "end": 2.0,
                "mode": "single",
                "tiles": [{
                    "rect": (0.0, 0.0, 1.0, 1.0),
                    "win": (608, 1080),
                    "keys": [(0.0, 500.0, 540.0), (2.0, 500.0, 540.0)],
                }],
            },
            {
                "start": 2.0,
                "end": 4.0,
                "mode": "split",
                "tiles": [
                    {
                        "rect": (0.0, 0.0, 1.0, 0.5),
                        "win": (960, 852),
                        "keys": [(2.0, 480.0, 426.0), (4.0, 480.0, 426.0)],
                    },
                    {
                        "rect": (0.0, 0.5, 1.0, 0.5),
                        "win": (960, 852),
                        "keys": [(2.0, 1440.0, 426.0), (4.0, 1440.0, 426.0)],
                    },
                ],
            },
        ],
    }

    graph = render.smart_filter(plan, 1080, 1920)

    assert "trim=start=0.000:end=2.000" in graph
    assert "xstack=inputs=2" in graph
    assert "concat=n=2:v=1:a=0[framed]" in graph


def test_close_webcams_in_a_call_split_evenly_not_reaction(monkeypatch):
    # A 640x360 two-person call: each webcam face is half the frame tall. The
    # 38% reaction strip could only show ~1.2 face-heights, cutting the
    # listener's chin for the whole clip.
    samples = [(i * 0.5, [_face(0.12, y=0.45, w=0.2, h=0.48),
                          _face(0.75, y=0.45, w=0.22, h=0.52)])
               for i in range(6)]
    words = [{"start": 0.0, "end": 2.8, "speaker": 0},
             {"start": 2.8, "end": 3.0, "speaker": 1}]
    monkeypatch.setattr(
        reframe, "detect_faces", lambda *args, **kwargs: (samples, (640, 360)))
    plan = reframe.plan("unused.mp4", 0.0, 3.0, 1080, 1920, speaker_words=words)
    scene = plan["scenes"][0]
    assert scene["mode"] == "split"
    assert [t["rect"][3] for t in scene["tiles"]] == [0.5, 0.5]


def test_crops_stay_inside_their_half_of_a_call():
    # The tall tile's resolution floor used to widen the crop past the seam, so
    # a strip of the other caller's feed ran down the frame edge.
    samples = [(i * 0.5, [_face(0.24, w=0.2, h=0.48), _face(0.74, w=0.2, h=0.48)])
               for i in range(6)]
    tracks = reframe.build_tracks(samples)
    regions = reframe._participant_regions(tracks, 640)
    assert sorted(regions.values()) == [(0.0, 320.0), (320.0, 640.0)]   # the seam
    for track in tracks:
        low, high = regions[id(track)]
        tile = reframe.tile_window(track, (0.0, 0.0, 1.0, 0.62),
                                   (low, high), 640, 360, 1080, 1920)
        w = tile["win"][0]
        assert w <= high - low
        for _, cx, _ in tile["keys"]:
            assert low - 1e-6 <= cx - w / 2 and cx + w / 2 <= high + 1e-6
