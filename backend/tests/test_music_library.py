from app.pipeline import music


def test_falls_back_to_generated_beds_without_a_library(tmp_path, monkeypatch):
    monkeypatch.setattr(music, "MUSIC_DIR", str(tmp_path))
    (tmp_path / "lofi.wav").write_bytes(b"x")
    assert music.path_for("podcast").endswith("lofi.wav")


def test_real_tracks_win_and_rotate_across_clips(tmp_path, monkeypatch):
    monkeypatch.setattr(music, "MUSIC_DIR", str(tmp_path))
    folder = tmp_path / "gaming"
    folder.mkdir()
    for name in ("a.mp3", "b.mp3", "notes.txt"):
        (folder / name).write_bytes(b"x")
    picks = [music.path_for("gaming", variant=i) for i in range(4)]
    assert [p.rsplit("/", 1)[-1] for p in picks] == ["a.mp3", "b.mp3", "a.mp3", "b.mp3"]


def test_empty_genre_folder_uses_the_general_library(tmp_path, monkeypatch):
    monkeypatch.setattr(music, "MUSIC_DIR", str(tmp_path))
    (tmp_path / "podcast").mkdir()
    (tmp_path / "general").mkdir()
    (tmp_path / "general" / "calm.mp3").write_bytes(b"x")
    assert music.path_for("podcast").endswith("general/calm.mp3")
