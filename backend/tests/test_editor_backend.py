"""Backend tests for the editor colour + captions_on refinement pass."""
import os
import time
import pytest
import requests

BASE_URL = os.environ.get(
    "REACT_APP_BACKEND_URL",
    "https://a989d36b-21b6-46b5-b93f-14b7756c1853.preview.emergentagent.com",
).rstrip("/")
API = f"{BASE_URL}/api"
JOB_ID = "demo-job-0001"
STORAGE_DIR = "/app/backend/storage"


@pytest.fixture(scope="module")
def client():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


# --- caption-options: vibrant/clean categories, >40 presets ------------------
def test_caption_options_categories_and_count(client):
    r = client.get(f"{API}/caption-options", timeout=30)
    assert r.status_code == 200
    data = r.json()
    cats = {c["id"] for c in data.get("categories", [])}
    assert "vibrant" in cats
    assert "clean" in cats
    presets = data.get("presets", [])
    assert isinstance(presets, list)
    assert len(presets) > 40, f"expected >40 presets, got {len(presets)}"
    # vibrant presets carry non-white base color
    vibrant = [p for p in presets if p["category"] == "vibrant"]
    assert len(vibrant) >= 3
    non_white = [p for p in vibrant if p["color"].lower() not in ("#ffffff", "#fff")]
    assert non_white, "vibrant presets should have coloured base text"


def test_job_exists(client):
    r = client.get(f"{API}/jobs/{JOB_ID}", timeout=15)
    assert r.status_code == 200
    j = r.json()
    assert j.get("status") == "done"
    assert len(j.get("clips", [])) >= 1


# --- PUT edit accepts new colour + captions_on fields ------------------------
def test_edit_accepts_new_fields(client):
    payload = {
        "start": 0.0,
        "end": 5.0,
        "caption_color": "#ff9000",
        "caption_active_color": "#00ffcc",
        "captions_on": True,
        "caption_style": "vibrant_sunset",
    }
    r = client.put(f"{API}/jobs/{JOB_ID}/clips/0/edit", json=payload, timeout=30)
    assert r.status_code == 200, r.text
    data = r.json()
    edit = data.get("edit") or {}
    assert edit.get("caption_color") == "#ff9000"
    assert edit.get("caption_active_color") == "#00ffcc"
    assert edit.get("captions_on") is True


# --- Export with captions on: ASS file gets recoloured PrimaryColour ---------
def test_export_captions_on_recolours_ass(client):
    payload = {
        "start": 0.0,
        "end": 5.0,
        "caption_color": "#ff9000",
        "caption_active_color": "#00ffcc",
        "captions_on": True,
        "caption_style": "vibrant_sunset",
    }
    r = client.put(f"{API}/jobs/{JOB_ID}/clips/0/edit", json=payload, timeout=30)
    assert r.status_code == 200

    r = client.post(f"{API}/jobs/{JOB_ID}/clips/0/export", timeout=180)
    assert r.status_code == 200, r.text

    ass_path = os.path.join(STORAGE_DIR, JOB_ID, "clip_1.ass")
    assert os.path.exists(ass_path), "captions on export should have written .ass"
    with open(ass_path, "r", encoding="utf-8") as f:
        contents = f.read()
    # PrimaryColour position on Style: line -- #ff9000 -> &H000090FF
    # Look for the substring within the Caption style line
    assert "&H000090FF" in contents, (
        "expected PrimaryColour &H000090FF (from #ff9000) in ASS"
    )


# --- Export with captions off: no .ass file should be present ---------------
def test_export_captions_off_writes_no_ass(client):
    ass_path = os.path.join(STORAGE_DIR, JOB_ID, "clip_1.ass")
    # Remove any leftover from prior test to make the assertion honest
    if os.path.exists(ass_path):
        os.remove(ass_path)

    payload = {
        "start": 0.0,
        "end": 5.0,
        "captions_on": False,
        "keyword_captions": False,     # captions off alone keeps keyword pop-ups
        "caption_style": "vibrant_sunset",
    }
    r = client.put(f"{API}/jobs/{JOB_ID}/clips/0/edit", json=payload, timeout=30)
    assert r.status_code == 200

    r = client.post(f"{API}/jobs/{JOB_ID}/clips/0/export", timeout=180)
    assert r.status_code == 200, r.text
    # Give FS a moment
    time.sleep(0.5)
    assert not os.path.exists(ass_path), (
        "with captions and keyword pop-ups off no .ass should be written"
    )


# --- Auth register sanity (used by the dashboard flow) ----------------------
def test_register_user(client):
    import uuid
    email = f"TEST_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post(f"{API}/auth/register",
                    json={"email": email, "password": "testpass123"}, timeout=15)
    assert r.status_code == 200, r.text
    data = r.json()
    assert "token" in data
    assert data["user"]["email"] == email.lower()
