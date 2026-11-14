from app.pipeline import content


def _samples(x, y, w=0.09, h=0.2, n=30):
    return [(t * 0.5, [(x, y, w, h, 0.9)]) for t in range(n)]


def test_centred_handheld_face_is_not_a_webcam():
    assert content.find_facecam(_samples(0.51, 0.36), 640, 360) is None


def test_corner_face_is_still_a_webcam():
    assert content.find_facecam(_samples(0.85, 0.8, w=0.08, h=0.12), 1920, 1080)
