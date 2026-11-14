from app.pipeline import reframe


def _row(xs, h=0.1, n=20):
    return [{"points": [(t * 0.5, x, 0.4, 0.04, h, 1.0) for t in range(n)],
             "last_t": (n - 1) * 0.5} for x in xs]


def test_wide_panel_row_is_split_and_stacked():
    scene = reframe._halves_scene(_row([0.15, 0.35, 0.5, 0.63, 0.8]), 20,
                                  0.0, 10.0, 1920, 1080, 1080, 1920)
    assert scene["mode"] == "split"
    top, bottom = scene["tiles"]
    # Windows do not overlap, so nobody appears twice.
    t_right = top["keys"][0][1] + top["win"][0] / 2
    b_left = bottom["keys"][0][1] - bottom["win"][0] / 2
    assert t_right <= b_left + 1


def test_medium_shot_or_small_group_keeps_old_layout():
    assert reframe._halves_scene(_row([0.3, 0.7]), 20,
                                 0, 10, 1920, 1080, 1080, 1920) is None
    assert reframe._halves_scene(_row([0.2, 0.4, 0.6, 0.8], h=0.35), 20,
                                 0, 10, 1920, 1080, 1080, 1920) is None


def test_three_person_medium_shot_fills_frame():
    scene = reframe._halves_scene(_row([0.25, 0.5, 0.75], h=0.22), 20,
                                  0, 10, 1920, 1080, 1080, 1920)
    assert scene and scene["mode"] == "split"
