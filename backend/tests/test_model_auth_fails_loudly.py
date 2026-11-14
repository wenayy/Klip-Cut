import pytest

from app.pipeline import analyzer


def test_rejected_key_fails_the_job_instead_of_mechanical_clips(monkeypatch):
    def refuse(*a, **k):
        raise analyzer.ModelAuthError("OpenAI rejected the API key.")
    monkeypatch.setattr(analyzer, "_openai_chat", refuse)
    monkeypatch.setattr(analyzer, "LLM_FALLBACK", True)
    utts = [{"start": i * 5.0, "end": i * 5.0 + 4.5, "transcript": "baat"} for i in range(40)]
    with pytest.raises(analyzer.ModelAuthError):
        analyzer.pick_clips(utts, 3)
