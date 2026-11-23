"""Step 4 core: publish settings, secrets store, redaction."""
from __future__ import annotations

import json
import os
import stat
import sys

import pytest
from pydantic import ValidationError

from autoclipper.config import Settings, load_settings, settings_path
from autoclipper.publish import secrets, service
from autoclipper.publish.settings import PublishSettings

POSIX = sys.platform != "win32"


# ---- settings ----------------------------------------------------------------------------------
def test_defaults_are_safe():
    ps = Settings().publish
    assert ps.dry_run is True
    assert ps.graph_api_version == "v27.0"
    for lang in ("en", "hi"):
        for p in ("youtube", "facebook", "instagram"):
            assert ps.target(lang, p).enabled is False
        assert ps.target(lang, "youtube").privacy == "private"
        assert ps.target(lang, "youtube").contains_synthetic_media is True
    assert ps.allow_same_account is False


def test_old_settings_json_without_publish_loads():
    raw = Settings().model_dump()
    del raw["publish"]
    settings_path().write_text(json.dumps(raw), encoding="utf-8")
    s = load_settings()
    assert s.publish == PublishSettings()


def test_whole_settings_round_trip_strict():
    s = Settings()
    again = Settings.model_validate(json.loads(json.dumps(s.model_dump())), strict=True)
    assert again == s


@pytest.mark.parametrize("bad", [
    {"dry_run": "yes"},
    {"graph_api_version": "v27.0\n"}, {"graph_api_version": "27.0"}, {"graph_api_version": "v100.0"},
    {"en": {"youtube": {"channel_id": "UC123"}}},
    {"en": {"youtube": {"channel_id": "UC" + "a" * 22 + "\n"}}},
    {"en": {"youtube": {"privacy": "secret"}}},
    {"en": {"youtube": {"category_id": "1234"}}}, {"en": {"youtube": {"category_id": "22\n"}}},
    {"hi": {"facebook": {"page_id": "12a"}}}, {"hi": {"facebook": {"page_id": "1" * 33}}},
    {"hi": {"instagram": {"ig_user_id": "-1"}}},
    {"chunk_mb": 0}, {"chunk_mb": 257}, {"max_retries": -1}, {"max_retries": 11},
    {"poll_interval_seconds": 0.5}, {"poll_timeout_seconds": 10},
    {"unknown": 1}, {"en": {"tiktok": {}}}, {"en": {"youtube": {"extra": True}}},
])
def test_invalid_publish_settings_rejected(bad):
    with pytest.raises(ValidationError):
        PublishSettings.model_validate(bad, strict=True)
    with pytest.raises(service.PublishInputError) as ei:
        service.put_publish_settings(bad)
    assert ei.value.errors


def test_put_publish_settings_replaces_only_publish_section():
    s = Settings()
    s.clips.count = 7
    settings_path().write_text(json.dumps(s.model_dump()), encoding="utf-8")
    body = PublishSettings().model_dump()
    body["dry_run"] = False
    body["hi"]["youtube"]["enabled"] = True
    body["hi"]["youtube"]["channel_id"] = "UC" + "x" * 22
    out = service.put_publish_settings(body)
    assert out["dry_run"] is False
    disk = json.loads(settings_path().read_text(encoding="utf-8"))
    assert disk["clips"]["count"] == 7
    assert disk["publish"]["hi"]["youtube"]["channel_id"] == "UC" + "x" * 22
    assert load_settings().publish.hi.youtube.enabled is True
    assert service.get_publish_settings() == out


def test_put_publish_settings_rejects_non_object_and_keeps_file():
    service.put_publish_settings(PublishSettings().model_dump())
    before = settings_path().read_bytes()
    for body in ([], "x", None, 3):
        with pytest.raises(service.PublishInputError):
            service.put_publish_settings(body)
    assert settings_path().read_bytes() == before


def test_target_lookup_rejects_unknown():
    ps = PublishSettings()
    with pytest.raises(KeyError):
        ps.target("fr", "youtube")
    with pytest.raises(KeyError):
        ps.target("en", "tiktok")


# ---- secrets -----------------------------------------------------------------------------------
def test_write_read_delete_secret_mode_0600():
    p = secrets.write_secret("meta_hi", {"page_id": "1", "page_token": "EAAsecretvalue123456789"})
    assert p.parent.name == "secrets"
    assert secrets.read_secret("meta_hi")["page_token"] == "EAAsecretvalue123456789"
    if POSIX:
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(p.parent).st_mode) == 0o700
    # no tmp files left behind
    assert [x.name for x in p.parent.iterdir()] == ["meta_hi.json"]
    assert secrets.delete_secret("meta_hi") is True
    assert secrets.delete_secret("meta_hi") is False
    assert secrets.read_secret("meta_hi") is None


def test_overwrite_keeps_0600_even_with_permissive_umask():
    old = os.umask(0o000)
    try:
        secrets.write_secret("youtube_en", {"refresh_token": "1//aaaaaaaaaaaaaaaa"})
        p = secrets.write_secret("youtube_en", {"refresh_token": "1//bbbbbbbbbbbbbbbb"})
    finally:
        os.umask(old)
    if POSIX:
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert secrets.read_secret("youtube_en")["refresh_token"] == "1//bbbbbbbbbbbbbbbb"


@pytest.mark.parametrize("name", ["../x", "settings", "youtube_fr", "meta", "resume_../../x_01_en_youtube",
                                  "resume_job_01_en_tiktok", ""])
def test_secret_name_allowlist(name):
    with pytest.raises(ValueError):
        secrets.write_secret(name, {})


def test_resume_names_allowed():
    secrets.write_secret("resume_20261123-120000-abc_01_hi_youtube", {"session_uri": "https://x/y?upload_id=zz12345678"})
    assert secrets.read_secret("resume_20261123-120000-abc_01_hi_youtube")


def test_unreadable_secret_is_missing_and_not_logged(caplog):
    p = secrets.secrets_dir() / "google_client.json"
    p.write_text('{"client_secret": "GOCSPX-broken', encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert secrets.read_secret("google_client") is None
    assert "GOCSPX" not in caplog.text
    p.write_text("[1, 2]", encoding="utf-8")
    assert secrets.read_secret("google_client") is None


def test_redact_known_values_and_patterns():
    secrets.write_secret("meta_en", {"page_token": "EAAredactme0123456789"})
    text = ("token EAAredactme0123456789 and Authorization: Bearer ya29.abc.def-123 and OAuth EAAzzz999 "
            "url?access_token=abc123&x=1 refresh_token=1//xyz upload_id=SESSION42 "
            '{"client_secret": "GOCSPX-s3cret"}')
    out = secrets.redact(text)
    for s in ("EAAredactme0123456789", "ya29.abc.def-123", "EAAzzz999", "abc123", "1//xyz", "SESSION42",
              "GOCSPX-s3cret"):
        assert s not in out, s
    assert "&x=1" in out
    assert secrets.redact("") == ""
    prose = "Google OAuth client not configured - upload client_secret.json; Bearer tokens expire"
    assert secrets.redact(prose) == prose


def test_publish_error_message_is_redacted():
    from autoclipper.publish.base import PublishError

    secrets.remember("EAAleakytoken123456789")
    e = PublishError("failed with EAAleakytoken123456789", kind="invalid", platform="facebook")
    assert "EAAleakytoken" not in str(e) and "***" in str(e)
    assert e.to_dict() == {"kind": "invalid", "message": str(e), "code": None, "retry_after": None}


def test_account_repr_hides_tokens():
    from autoclipper.publish.accounts import Account

    a = Account({"id": "1", "page_token": "EAAhidden0000000000", "refresh_token": "1//hidden00000000"})
    assert "hidden" not in repr(a) and "hidden" not in str(a)
    assert a.public == {"id": "1", "name": None}
