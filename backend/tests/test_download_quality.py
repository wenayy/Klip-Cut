"""The video download asks for HD, and 360p is only the last resort."""

from app.pipeline import downloader


def test_height_cap_is_passed_and_android_is_last(monkeypatch):
    calls = []

    def fake_run(cmd, on_progress):
        calls.append(cmd)
        if len(calls) < 3:
            raise RuntimeError("HTTP Error 403")

    monkeypatch.setattr(downloader, "_run_with_progress", fake_run)
    monkeypatch.setattr(downloader, "_source_height", lambda path: 1080)
    downloader.download_video("https://youtu.be/x", "/tmp/out.mp4", max_height=720)
    assert all(downloader._video_format(720) in c for c in calls)
    clients = [c[c.index("--extractor-args") + 1] if "--extractor-args" in c else None
               for c in calls]
    assert clients == [None, "youtube:player_client=android_vr,tv_simply",
                       "youtube:player_client=android"]
