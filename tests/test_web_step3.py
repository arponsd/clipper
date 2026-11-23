"""Step 3 web: clip-detail caption fields, HyperFrames settings round-trip + 422s, doctor rows, static UI."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_web_support import (BASE, api, assert_error_shape, assert_keys, make_client,  # noqa: F401
                              make_job_on_disk, read_json)

from autoclipper.cli import Check
from autoclipper.config import Settings, save_settings, settings_path
from autoclipper.jobstore import JobStore
from autoclipper.web.auth import TOKEN_HEADER


def _stamp(store: JobStore, clip: int, lang: str, **data) -> None:
    (store.clip_dir(clip, lang) / "render.json").write_text(json.dumps(data), encoding="utf-8")


def _set_engine(client: TestClient, engine: str, **hfkw) -> dict:
    s = client.get("/api/settings").json()
    s["captions"]["engine"] = engine
    s["hyperframes"].update(hfkw)
    r = client.put("/api/settings", json=s)
    assert r.status_code == 200, r.text
    return r.json()


def _langs(client: TestClient, job_id: str) -> dict:
    r = client.get(f"/api/jobs/{job_id}")
    assert r.status_code == 200
    return r.json()["clips"][0]["langs"]


def test_clip_detail_caption_fields_three_cases(api):
    client, _, _ = api
    store = make_job_on_disk(clips=1)
    # en: legacy final without render.json; hi: fallback stamp + warning
    _stamp(store, 0, "hi", caption_engine="hyperframes", engine_used="ass", style="pop", format="webm", fps=None,
           warning="HyperFrames captions failed (node not found); used ASS captions")
    store.job.clips[0].langs["hi"].warning = "HyperFrames captions failed (node not found); used ASS captions"
    store.save()

    langs = _langs(client, store.job.id)
    for cl in langs.values():
        assert_keys({k: cl[k] for k in ("warning", "caption_engine", "caption_style", "render_stale")},
                    {"warning": (str, type(None)), "caption_engine": (str, type(None)),
                     "caption_style": (str, type(None)), "render_stale": bool})
    en, hi = langs["en"], langs["hi"]
    # 1. no stamp, engine ass: an ASS render, current
    assert en["warning"] is None and en["caption_engine"] == "ass" and en["render_stale"] is False
    # 2. fallback stamp: engine used ass, warning shown
    assert hi["warning"].startswith("HyperFrames captions failed") and hi["caption_engine"] == "ass"
    assert hi["render_stale"] is True  # the stamp's caption_engine (hyperframes) differs from the setting (ass)

    # 3. stale engine: switch the setting to hyperframes -> the ASS render is stale. The fallback render
    #    matches the settings it was made with, so it shows only its warning, not "re-render to apply"
    #    (triage R3, correctness reviewer); resume still retries it (render_is_current stays False).
    _set_engine(client, "hyperframes")
    langs = _langs(client, store.job.id)
    assert langs["en"]["render_stale"] is True
    assert langs["hi"]["render_stale"] is False and langs["hi"]["warning"].startswith("HyperFrames captions failed")
    from autoclipper.config import load_settings
    from autoclipper.stages.render import render_is_current
    assert not render_is_current(store.clip_dir(0, "hi"), load_settings())

    # a good hyperframes stamp is current and reports its style
    _stamp(store, 0, "en", caption_engine="hyperframes", engine_used="hyperframes", style="pop", format="webm",
           fps=None, warning=None)
    en = _langs(client, store.job.id)["en"]
    assert en["caption_engine"] == "hyperframes" and en["caption_style"] == "pop" and en["render_stale"] is False
    _set_engine(client, "hyperframes", style="karaoke")
    assert _langs(client, store.job.id)["en"]["render_stale"] is True


def test_clip_detail_no_final_has_null_engine(api):
    client, _, _ = api
    store = make_job_on_disk(clips=1, final=False)
    for cl in _langs(client, store.job.id).values():
        assert cl["caption_engine"] is None and cl["caption_style"] is None and cl["render_stale"] is False
        assert cl["warning"] is None


def test_clip_detail_survives_broken_settings(api):
    client, _, _ = api
    store = make_job_on_disk(clips=1)
    settings_path().write_text("{not json", encoding="utf-8")
    r = client.get(f"/api/jobs/{store.job.id}")
    assert r.status_code == 200
    assert r.json()["clips"][0]["langs"]["en"]["render_stale"] is False


def test_settings_schema_has_engine_and_hyperframes(api):
    client, _, _ = api
    schema = client.get("/api/settings/schema").json()
    assert "hyperframes" in schema["properties"]
    defs = schema["$defs"]
    assert defs["CaptionSettings"]["properties"]["engine"]["enum"] == ["ass", "hyperframes"]
    hp = defs["HyperFramesSettings"]["properties"]
    assert set(hp) == {"node_bin_dir", "package_dir", "style", "fps", "format", "workers", "timeout_seconds"}
    assert hp["style"]["enum"] == ["pop", "karaoke"] and hp["format"]["enum"] == ["webm", "mov"]


def test_put_settings_round_trips_engine_and_fps_null(api):
    client, _, _ = api
    s = client.get("/api/settings").json()
    assert s["captions"]["engine"] == "ass" and s["hyperframes"]["fps"] is None
    s["captions"]["engine"] = "hyperframes"
    s["hyperframes"].update(style="karaoke", fps=30, format="mov", workers=2, timeout_seconds=120,
                            node_bin_dir="%USERPROFILE%\\tools\\node22", package_dir="~/tools/hyperframes")
    r = client.put("/api/settings", json=s)
    assert r.status_code == 200, r.text
    assert r.json() == s and read_json(settings_path()) == s
    s["hyperframes"]["fps"] = None
    r = client.put("/api/settings", json=s)
    assert r.status_code == 200 and r.json()["hyperframes"]["fps"] is None
    assert read_json(settings_path())["hyperframes"]["fps"] is None
    assert read_json(settings_path())["hyperframes"]["node_bin_dir"] == "%USERPROFILE%\\tools\\node22"  # as typed


@pytest.mark.parametrize("section,key,value", [
    ("hyperframes", "style", "x"),
    ("hyperframes", "fps", 5),
    ("hyperframes", "fps", 61),
    ("hyperframes", "fps", 0),
    ("hyperframes", "fps", "30"),
    ("hyperframes", "timeout_seconds", 1),
    ("hyperframes", "timeout_seconds", -5),
    ("hyperframes", "timeout_seconds", 10 ** 9),
    ("hyperframes", "workers", 99),
    ("hyperframes", "workers", -1),
    ("hyperframes", "format", "mp4"),
    ("hyperframes", "node_bin_dir", "a" * 501),
    ("hyperframes", "package_dir", "a\nb"),
    ("hyperframes", "node_bin_dir", 5),
    ("hyperframes", "bogus", 1),
    ("captions", "engine", "libass"),
    ("captions", "engine", ""),
    ("captions", "engine", None),
])
def test_put_settings_hyperframes_validation_422(api, section, key, value):
    client, _, _ = api
    before = settings_path().read_bytes()
    s = client.get("/api/settings").json()
    s[section][key] = value
    body = assert_error_shape(client.put("/api/settings", json=s), 422, "validation failed")
    assert f"{section}.{key}" in body["errors"], body["errors"]
    assert settings_path().read_bytes() == before


def test_put_settings_hyperframes_auth(api):
    client, _, _ = api
    s = client.get("/api/settings").json()
    s["captions"]["engine"] = "hyperframes"
    bare = TestClient(client.app, base_url=BASE)
    assert_error_shape(bare.put("/api/settings", json=s), 401)
    assert_error_shape(bare.put("/api/settings", json=s, headers={TOKEN_HEADER: "x"}), 403)
    assert json.loads(settings_path().read_text(encoding="utf-8"))["captions"]["engine"] == "ass"


def test_doctor_route_shows_hyperframes_rows():
    def doctor(settings: Settings):
        from autoclipper import cli

        return [Check("python", "ok", "3.12")] + [Check(n, "warn", "missing") for n in
                                                   ("node", "hyperframes", "hyperframes chrome", "caption engine")]

    client, _, _ = make_client(doctor=doctor)
    r = client.get("/api/doctor")
    assert r.status_code == 200
    names = [c["name"] for c in r.json()["checks"]]
    assert names[1:] == ["node", "hyperframes", "hyperframes chrome", "caption engine"]


def test_doctor_route_with_real_check_hyperframes(monkeypatch: pytest.MonkeyPatch):
    """The web route reuses cli.run_doctor: with nothing installed under the (tmp) HOME the HyperFrames
    rows are warnings, never failures, and nothing touches the network."""
    from autoclipper import cli

    client, _, _ = make_client(doctor=lambda s: cli.check_hyperframes(s))
    r = client.get("/api/doctor")
    assert r.status_code == 200
    rows = {c["name"]: c for c in r.json()["checks"]}
    assert rows["node"]["level"] == "warn" and rows["hyperframes"]["level"] == "warn"
    assert rows["caption engine"]["level"] == "ok"
    assert all(c["level"] != "fail" for c in r.json()["checks"])


def test_static_ui_has_hyperframes_labels_and_banner(api):
    client, _, _ = api
    js = client.get("/static/app.js").text
    for text in ("'captions.engine'", "'hyperframes.node_bin_dir'", "Folder with node (Node 22+)",
                 "'hyperframes.package_dir'", "npm --prefix", "'hyperframes.fps'", "same as the clip",
                 "Caption engine/style changed - re-render to apply",
                 "Captions: HyperFrames", "Captions: ASS", "is-inactive"):
        assert text in js, text
    assert "is-inactive" in client.get("/static/styles.css").text


def test_fallback_warning_is_shown_verbatim_in_the_ui():
    """Triage R3: cl.warning already reads "HyperFrames captions failed (...); used ASS captions"; app.js
    must not put a second copy of that sentence in front of it."""
    js = (Path(__file__).resolve().parents[1] / "autoclipper" / "web" / "static" / "app.js").read_text(encoding="utf-8")
    assert "ASS captions were used: '" not in js
    assert "text: cl.warning }" in js and "text: entry.warning }" in js
