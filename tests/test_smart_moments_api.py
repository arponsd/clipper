"""API, ingest and CLI parts of the video context + smart moments features (global API checklist: happy path,
validation 422, 401/403, 404, response contract, side effects). No network: yt-dlp is faked."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
import yt_dlp
from fakes import FakeLLM, FakeTranscriber, FakeVoicebox, make_video
from fastapi.testclient import TestClient
from test_web_support import (BASE, api, api_idle, assert_error_shape, assert_keys, make_job_on_disk,  # noqa: F401
                              read_json)

from autoclipper.cli import main
from autoclipper.config import Settings, load_settings, save_settings, settings_path
from autoclipper.jobstore import JobStore, jobs_root
from autoclipper.models import Job
from autoclipper.pipeline import Deps, run_job
from autoclipper.stages import source_info as si
from autoclipper.stages.ingest import ingest
from autoclipper.web.auth import TOKEN_HEADER

SOURCE_INFO_KEYS = {"kind": str, "title": str, "uploader": str, "channel": str, "url": str, "upload_date": str,
                    "site": str, "filename": str, "duration": (float, int, type(None)), "categories": list,
                    "tags": list, "description": str, "notes": str, "game": bool}
NOTES = "GTA 6 gameplay by Nemsk.\nRobbing every bank without stopping."


# --------------------------------------------------------------------------------------------- POST /api/jobs
def test_create_job_with_notes_happy_path_and_side_effects(api_idle):
    client, _, _ = api_idle
    r = client.post("/api/jobs", json={"url": "https://www.youtube.com/watch?v=MqFGfn22BmY",
                                       "options": {"clips": 6, "notes": NOTES}})
    assert r.status_code == 201, r.text
    job_id = r.json()["id"]
    jdir = jobs_root() / job_id
    info = read_json(jdir / "source_info.json")
    assert info["notes"] == NOTES and info["kind"] == "url"
    assert info["url"] == "https://www.youtube.com/watch?v=MqFGfn22BmY"
    job = Job.model_validate_json((jdir / "job.json").read_text(encoding="utf-8"))
    assert job.source_info is None  # set by the worker's ingest stage (request threads save job.json once)
    assert "notes" not in read_json(jdir / "options.json")  # notes live in source_info.json only
    d = client.get(f"/api/jobs/{job_id}").json()
    assert_keys(d["source_info"], SOURCE_INFO_KEYS)
    assert d["source_info"]["notes"] == NOTES and d["source_info"]["game"] is True  # "GTA 6" in the notes
    assert set(d["stages"]) >= {"ingest", "transcribe", "signals", "moments", "cut"}
    assert d["stages"]["signals"]["status"] == "pending"


def test_create_job_from_upload_with_notes(api_idle):
    client, _, _ = api_idle
    up = client.post("/api/uploads?filename=My Talk.MOV", content=b"\0" * 100,
                     headers={"Content-Type": "application/octet-stream"}).json()
    r = client.post("/api/jobs", json={"upload_id": up["upload_id"], "options": {"notes": "A talk"}})
    assert r.status_code == 201, r.text
    info = read_json(jobs_root() / r.json()["id"] / "source_info.json")
    assert info["kind"] == "upload" and info["filename"] == "My Talk.MOV" and info["notes"] == "A talk"


def test_create_job_without_notes_still_records_the_source(api_idle):
    client, _, _ = api_idle
    r = client.post("/api/jobs", json={"url": "https://example.com/v"})
    assert r.status_code == 201
    info = read_json(jobs_root() / r.json()["id"] / "source_info.json")
    assert info["notes"] == "" and info["url"] == "https://example.com/v"


@pytest.mark.parametrize("notes", ["x" * 1001, 5, ["a"], "bad\x00nul", "bell\x07", True])
def test_create_job_notes_validation_422(api, notes):
    client, _, _ = api
    r = client.post("/api/jobs", json={"url": "https://a.b/v", "options": {"notes": notes}})
    body = assert_error_shape(r, 422, "validation failed")
    assert "options.notes" in body["errors"], body
    assert JobStore.list_ids() == []  # nothing created


def test_create_job_notes_boundaries_ok(api_idle):
    client, _, _ = api_idle
    for notes in ("", "x" * 1000, "tabs\tand\r\nnewlines", "हिन्दी नोट्स"):
        r = client.post("/api/jobs", json={"url": "https://a.b/v", "options": {"notes": notes}})
        assert r.status_code == 201, (notes, r.text)


def test_create_job_with_notes_auth_401_403(api):
    client, _, _ = api
    bare = TestClient(client.app, base_url=BASE)
    body = {"url": "https://a.b/v", "options": {"notes": NOTES}}
    assert_error_shape(bare.post("/api/jobs", json=body), 401)
    assert_error_shape(bare.post("/api/jobs", json=body, headers={TOKEN_HEADER: "bad"}), 403)
    assert JobStore.list_ids() == []


def test_create_job_with_notes_unknown_upload_404(api):
    client, _, _ = api
    r = client.post("/api/jobs", json={"upload_id": "0" * 32, "options": {"notes": NOTES}})
    assert_error_shape(r, 404, f"upload {'0' * 32} not found")
    assert JobStore.list_ids() == []


def test_idempotency_key_covers_the_notes(api_idle):
    client, _, _ = api_idle
    h = {"Idempotency-Key": "k-notes-1"}
    body = {"url": "https://a.b/v", "options": {"notes": "one"}}
    first = client.post("/api/jobs", json=body, headers=h)
    assert first.status_code == 201
    again = client.post("/api/jobs", json=body, headers=h)
    assert again.status_code == 200 and again.json()["id"] == first.json()["id"]
    other = client.post("/api/jobs", json={"url": "https://a.b/v", "options": {"notes": "two"}}, headers=h)
    assert_error_shape(other, 409)
    assert len(JobStore.list_ids()) == 1


# --------------------------------------------------------------------------------------------- GET /api/jobs/{id}
def test_detail_of_older_job_has_no_context_and_signals_not_used(api):
    client, _, _ = api
    store = make_job_on_disk()
    store.job.stages.pop("signals", None)  # as written before the signals stage existed
    for st in store.job.stages.values():
        st.status = "done"
    store.save()
    d = client.get(f"/api/jobs/{store.job.id}").json()
    assert d["source_info"] is None
    assert d["stages"]["signals"] == {"status": "skipped", "progress": 1.0, "message": "not used (older job)"}


def test_detail_unknown_job_404_and_bad_id_422(api):
    client, _, _ = api
    assert_error_shape(client.get("/api/jobs/20990101-000000-abcdef"), 404)
    assert client.get("/api/jobs/bad.id").status_code == 422


# --------------------------------------------------------------------------------------------- PUT /api/settings
def test_settings_signals_and_context_happy_path_persisted(api):
    client, _, _ = api
    s = client.get("/api/settings").json()
    assert {"signals", "context"} <= set(s)
    s["signals"].update(enabled=False, sample_fps=2.0, window_seconds=15.0, speech_rich_coverage=0.3)
    s["context"].update(credit_source=False, description_chars=500)
    r = client.put("/api/settings", json=s)
    assert r.status_code == 200, r.text
    saved = json.loads(settings_path().read_text(encoding="utf-8"))
    assert saved["signals"]["enabled"] is False and saved["signals"]["sample_fps"] == 2.0
    assert saved["context"] == {"enabled": True, "credit_source": False, "description_chars": 500}
    assert load_settings().signals.window_seconds == 15.0
    schema = client.get("/api/settings/schema").json()
    assert "SignalSettings" in schema["$defs"] and "ContextSettings" in schema["$defs"]


@pytest.mark.parametrize("section,key,value", [
    ("signals", "sample_fps", 0), ("signals", "sample_fps", 11), ("signals", "window_seconds", 4),
    ("signals", "speech_rich_coverage", 1.5), ("signals", "speech_rich_coverage", -0.1),
    ("signals", "static_motion", "high"), ("signals", "enabled", "yes"), ("signals", "sample_width", 16),
    ("signals", "reject_static_fraction", 0), ("signals", "cut_threshold", 2), ("signals", "menu_flatness", 0.1),
    ("signals", "timeout_seconds", 5), ("signals", "bogus", 1),
    ("context", "description_chars", 1001), ("context", "description_chars", -1), ("context", "enabled", None),
])
def test_settings_signals_validation_422(api, section, key, value):
    client, _, _ = api
    before = settings_path().read_text(encoding="utf-8")
    s = client.get("/api/settings").json()
    s[section][key] = value
    body = assert_error_shape(client.put("/api/settings", json=s), 422, "validation failed")
    assert any(k.startswith(f"{section}.{key}") for k in body["errors"]), body
    assert settings_path().read_text(encoding="utf-8") == before  # nothing written


def test_settings_auth_401_403(api):
    client, _, _ = api
    bare = TestClient(client.app, base_url=BASE)
    s = client.get("/api/settings").json()
    assert_error_shape(bare.put("/api/settings", json=s), 401)
    assert_error_shape(bare.put("/api/settings", json=s, headers={TOKEN_HEADER: "bad"}), 403)


# --------------------------------------------------------------------------------------------- ingest + pipeline
YT_INFO = {"title": "Robbing EVERY BANK in GTA 6 Without Stopping", "uploader": "Nemsk",
           "webpage_url": "https://www.youtube.com/watch?v=MqFGfn22BmY", "description": "My GTA 6 video",
           "tags": ["gta 6", "gta vi"], "categories": ["Gaming"], "duration": 20, "upload_date": "20261120",
           "extractor_key": "Youtube"}


@pytest.fixture
def ydl_with_info(monkeypatch, tmp_path):
    sample = make_video(tmp_path / "sample.mp4", seconds=20.0)

    class YDL:
        calls: list[str] = []

        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=True):
            YDL.calls.append("extract_info")
            shutil.copy(sample, Path(self.opts["outtmpl"].replace("%(ext)s", "mp4")))
            return dict(YT_INFO)

        def download(self, urls):
            YDL.calls.append("download")
            shutil.copy(sample, Path(self.opts["outtmpl"].replace("%(ext)s", "mp4")))
            return 0

    YDL.calls = []
    monkeypatch.setattr(yt_dlp, "YoutubeDL", YDL)
    return YDL


def test_ingest_hands_the_page_info_to_on_info(ydl_with_info, tmp_path):
    got: dict = {}
    ingest("https://www.youtube.com/watch?v=MqFGfn22BmY", tmp_path / "job", on_info=got.update)
    assert got["uploader"] == "Nemsk" and ydl_with_info.calls == ["extract_info"]
    ingest("https://x.y/z", tmp_path / "job2")  # no callback: the plain download as before
    assert ydl_with_info.calls == ["extract_info", "download"]


def test_url_job_stores_page_info_and_keeps_notes(ydl_with_info):
    s = Settings(languages=["en"], voiceover=False)
    s.clips.count, s.clips.min_seconds, s.clips.max_seconds = 1, 10, 15
    save_settings(s)
    llm = FakeLLM()
    store = run_job("https://www.youtube.com/watch?v=MqFGfn22BmY", settings=s, notes="my notes",
                    deps=Deps(llm=llm, voicebox=FakeVoicebox(), transcriber=FakeTranscriber(duration=20.0)),
                    log=lambda _l: None)
    assert store.job.status == "done", store.job.error
    info = si.read_source_info(store.dir)
    assert info.title.startswith("Robbing") and info.uploader == "Nemsk" and info.notes == "my notes"
    assert info.looks_like_game() and info.upload_date == "2026-11-20"
    meta = read_json(store.dir / "clips" / "00" / "en" / "metadata.json")
    # FakeLLM's description has no credit: the pipeline added it
    assert meta["description"].endswith("Original video by Nemsk: https://www.youtube.com/watch?v=MqFGfn22BmY")


# --------------------------------------------------------------------------------------------- CLI
def test_cli_run_notes_too_long_is_a_usage_error(capsys):
    assert main(["run", "https://a.b/v", "--notes", "x" * 1001]) == 2
    assert "at most 1000" in capsys.readouterr().err
    assert JobStore.list_ids() == []


def test_cli_context_show_set_clear(capsys):
    store = make_job_on_disk()
    assert main(["context", store.job.id]) == 0
    assert "no source context recorded" in capsys.readouterr().out
    assert main(["context", store.job.id, "--notes", "GTA 6 gameplay by Nemsk"]) == 0
    out = capsys.readouterr().out
    assert "notes: GTA 6 gameplay by Nemsk" in out and "detected: video game footage" in out
    assert JobStore.load(store.job.id).job.source_info == "source_info.json"
    assert main(["context", store.job.id, "--clear-notes"]) == 0
    assert si.read_source_info(store.dir).notes == ""
    assert main(["context", "20990101-000000-abcdef"]) == 1
    assert "not found" in capsys.readouterr().err


def test_cli_context_fetch(monkeypatch, capsys):
    store = make_job_on_disk()
    store.job.source = "https://www.youtube.com/watch?v=MqFGfn22BmY"
    store.save()
    si.set_notes(store.dir, "keep me", source=store.job.source)
    seen: list[str] = []

    def fake_fetch(url, *, description_chars=1000):
        seen.append(url)
        return si.from_ytdlp(YT_INFO, url, description_chars=description_chars)

    monkeypatch.setattr(si, "fetch_url_info", fake_fetch)
    assert main(["context", store.job.id, "--fetch"]) == 0
    info = si.read_source_info(store.dir)
    assert seen == ["https://www.youtube.com/watch?v=MqFGfn22BmY"]
    assert info.uploader == "Nemsk" and info.notes == "keep me"
    # an upload job has no page to fetch
    up = make_job_on_disk()
    up.job.source = "upload:a.mp4"
    up.save()
    assert main(["context", up.job.id, "--fetch"]) == 1
    assert "not a URL" in capsys.readouterr().err


def test_cli_signals_prints_summary_and_table(tmp_path, capsys):
    store = make_job_on_disk()
    make_video(store.dir / "source.mp4", seconds=12.0)
    (store.dir / "transcript.json").write_text(json.dumps(
        {"language": "en", "duration": 12.0, "segments": []}), encoding="utf-8")
    assert main(["signals", store.job.id]) == 0
    out = capsys.readouterr().out
    assert "scene cuts" in out and "speech coverage 0%" in out and "[0-10]" in out
    assert (store.dir / "signals.json").is_file()


def test_cli_signals_respects_the_job_lock_and_reports_signal_errors(tmp_path, capsys, monkeypatch):
    from autoclipper import joblock
    from autoclipper.stages import signals as S

    store = make_job_on_disk()
    make_video(store.dir / "source.mp4", seconds=6.0)
    monkeypatch.setattr(joblock, "lock_is_live", lambda cur: bool(cur))
    joblock.lock_path(store.job.id).write_text(json.dumps({"pid": os.getpid() + 1, "host": "x", "started": 1}),
                                               encoding="utf-8")
    assert main(["signals", store.job.id]) == 1
    assert "being run by another process" in capsys.readouterr().err
    assert not (store.dir / "signals.json").exists()  # no second decode next to the running job
    joblock.lock_path(store.job.id).unlink()

    def boom(*a, **k):
        raise S.SignalsError("decoder exploded")

    monkeypatch.setattr(S, "compute_signals", boom)
    assert main(["signals", store.job.id]) == 1
    assert "could not compute the signals: decoder exploded" in capsys.readouterr().err
