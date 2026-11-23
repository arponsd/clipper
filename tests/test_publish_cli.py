"""Step 4 core: `autoclipper publish` and `autoclipper accounts` CLI."""
from __future__ import annotations

import json

import pytest
from publish_fakes import (CHANNEL, IG, PAGE, PAGE_TOKEN, FakeNet, client_secret_json, connect_all, grep_secrets,
                           make_job)

from autoclipper import cli
from autoclipper.config import load_settings
from autoclipper.publish import base, records, secrets, service


@pytest.fixture
def net(monkeypatch) -> FakeNet:
    n = FakeNet()
    monkeypatch.setattr(base, "new_client", n.client)
    monkeypatch.setattr(service, "new_client", n.client)
    monkeypatch.setattr("time.sleep", lambda s: None)
    return n


def run_cli(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_parser_publish_and_accounts():
    p = cli.build_parser()
    a = p.parse_args(["publish", "J", "--clip", "1", "--lang", "hi", "--platform", "youtube", "--platform", "all",
                      "--dry-run"])
    assert a.platform == ["youtube", "all"] and a.dry_run and not a.yes and not a.force and a.clip == 1
    a = p.parse_args(["accounts"])
    assert a.func.__name__ == "cmd_accounts_status"
    a = p.parse_args(["accounts", "connect", "meta", "--lang", "en", "--page-id", "1", "--ig-user-id", "2"])
    assert a.target == "meta" and a.page_id == "1"
    a = p.parse_args(["accounts", "disconnect", "meta", "--lang", "hi", "--no-revoke"])
    assert a.no_revoke


@pytest.mark.parametrize("argv", [
    ["publish", "J", "--clip", "1", "--lang", "hi"],  # no platform
    ["publish", "J", "--clip", "1", "--lang", "fr", "--platform", "youtube"],
    ["publish", "J", "--clip", "-1", "--lang", "hi", "--platform", "youtube"],
    ["publish", "J", "--clip", "1", "--lang", "hi", "--platform", "tiktok"],
    ["accounts", "connect", "youtube"],  # --lang required
    ["accounts", "connect", "meta", "--lang", "hi", "--page-token", "EAAxxxx"],  # tokens never via argv
])
def test_usage_errors_exit_2(capsys, argv):
    code, _, _ = run_cli(capsys, *argv)
    assert code == 2


def test_dry_run_prints_plan_without_network(capsys, monkeypatch):
    job_id = make_job()
    connect_all(dry_run=True)
    monkeypatch.setattr(base, "new_client", lambda: (_ for _ in ()).throw(AssertionError("network")))
    code, out, err = run_cli(capsys, "publish", job_id, "--clip", "1", "--lang", "hi", "--platform", "all")
    assert code == 0, err
    assert "== YouTube (Hindi) - API v3" in out and "== Facebook (Hindi) - API v27.0" in out
    assert "POST https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable" in out
    assert "ready: this request plan would be sent" in out
    assert "Dry run is ON in settings: nothing was posted" in out
    assert grep_secrets(out, err) == []
    for p in ("youtube", "facebook", "instagram"):
        assert json.loads(records.plan_path(job_id, 1, "hi", p).read_text(encoding="utf-8"))["dry_run"] is True


def test_forced_dry_run_while_live_settings(capsys, net):
    job_id = make_job()
    connect_all(dry_run=False)
    code, out, _ = run_cli(capsys, "publish", job_id, "--clip", "1", "--lang", "en", "--platform", "youtube",
                           "--dry-run")
    assert code == 0 and "Dry run: nothing was posted." in out
    assert net.requests == []


def test_live_refused_without_yes(capsys, net):
    job_id = make_job()
    connect_all(dry_run=False)
    code, out, err = run_cli(capsys, "publish", job_id, "--clip", "1", "--lang", "hi", "--platform", "youtube",
                             "--platform", "facebook")
    assert code == 2 and "add --yes" in err
    assert "This would post FOR REAL" in out and f"{CHANNEL['hi'][1]}" in out and "privacy private" in out
    assert net.requests == []


def test_live_not_ready_exit_2(capsys, net):
    job_id = make_job()
    code, _, err = run_cli(capsys, "publish", job_id, "--clip", "1", "--lang", "hi", "--platform", "youtube",
                           "--dry-run")
    assert code == 0  # dry run always works
    connect_all(dry_run=False, enable=False)
    code, _, err = run_cli(capsys, "publish", job_id, "--clip", "1", "--lang", "hi", "--platform", "youtube",
                           "--yes")
    assert code == 2 and "error: cannot publish" in err and "disabled" in err
    code, _, err = run_cli(capsys, "publish", "no-such-job", "--clip", "1", "--lang", "hi", "--platform",
                           "youtube", "--dry-run")
    assert code == 2 and "not found" in err and "Traceback" not in err
    assert net.requests == []


def test_live_publish_with_yes_then_already_published(capsys, net):
    job_id = make_job()
    connect_all(dry_run=False)
    code, out, err = run_cli(capsys, "publish", job_id, "--clip", "1", "--lang", "hi", "--platform", "all", "--yes")
    assert code == 0, err
    assert "YouTube: published https://www.youtube.com/shorts/" in out
    assert "Facebook: published https://www.facebook.com/reel/" in out
    assert "Instagram: published https://www.instagram.com/reel/" in out
    assert grep_secrets(out, err) == []
    n = len(net.requests)
    code, out, _ = run_cli(capsys, "publish", job_id, "--clip", "1", "--lang", "hi", "--platform", "youtube", "--yes")
    assert code == 0 and "already published" in out and len(net.requests) == n


def test_live_failure_exit_1(capsys, net):
    from publish_fakes import google_error
    job_id = make_job()
    connect_all(dry_run=False)
    net.google.inject["start"] = [google_error(403, "quotaExceeded")]
    code, out, _ = run_cli(capsys, "publish", job_id, "--clip", "1", "--lang", "hi", "--platform", "youtube", "--yes")
    assert code == 1 and "YouTube: FAILED (quota): YouTube daily upload quota used up" in out


def test_accounts_status_and_google_client(capsys, tmp_path):
    f = tmp_path / "client_secret.json"
    f.write_text(json.dumps(client_secret_json()), encoding="utf-8")
    code, out, _ = run_cli(capsys, "accounts", "google-client", str(f))
    assert code == 0 and "12345678…apps.googleusercontent.com" in out
    assert secrets.read_secret("google_client")
    connect_all()
    code, out, _ = run_cli(capsys, "accounts")
    assert code == 0
    assert "dry run: OFF" in out
    assert f"hi  YouTube    connected     enabled  -> {CHANNEL['hi'][1]} ({CHANNEL['hi'][0]})" in out
    assert grep_secrets(out) == []
    bad = tmp_path / "web.json"
    bad.write_text(json.dumps(client_secret_json("web")), encoding="utf-8")
    code, _, err = run_cli(capsys, "accounts", "google-client", str(bad))
    assert code == 2 and "Desktop app" in err
    code, _, err = run_cli(capsys, "accounts", "google-client", str(tmp_path / "missing.json"))
    assert code == 2


def test_connect_meta_reads_tokens_from_env_or_getpass(capsys, net, monkeypatch):
    monkeypatch.setenv("AUTOCLIPPER_PAGE_TOKEN", PAGE_TOKEN["hi"])
    asked = []
    monkeypatch.setattr("getpass.getpass", lambda prompt: asked.append(prompt) or "")
    code, out, err = run_cli(capsys, "accounts", "connect", "meta", "--lang", "hi", "--page-id", PAGE["hi"][0],
                             "--ig-user-id", IG["hi"][0])
    assert code == 0, err
    assert f"Facebook (Hindi): connected {PAGE['hi'][1]}" in out and f"Instagram (Hindi): connected {IG['hi'][1]}" in out
    assert len(asked) == 1 and "Instagram token" in asked[0]  # page token came from env
    assert grep_secrets(out, err) == []
    monkeypatch.delenv("AUTOCLIPPER_PAGE_TOKEN")
    monkeypatch.setattr("getpass.getpass", lambda prompt: "short")
    code, _, err = run_cli(capsys, "accounts", "connect", "meta", "--lang", "en", "--page-id", PAGE["en"][0])
    assert code == 2 and "page_token" in err
    code, out, _ = run_cli(capsys, "accounts", "disconnect", "meta", "--lang", "hi")
    assert code == 0 and secrets.read_secret("meta_hi") is None


def test_connect_youtube_loopback_flow(capsys, net, monkeypatch):
    """The CLI flow serves a one-shot loopback page; simulate the browser hitting it."""
    import threading
    import urllib.parse
    import urllib.request

    from publish_fakes import AUTH_CODE
    service.set_google_client(client_secret_json())

    def browser(url):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        cb = f"{q['redirect_uri']}?state={q['state']}&code={urllib.parse.quote(AUTH_CODE)}"
        threading.Thread(target=lambda: urllib.request.urlopen(cb, timeout=5).read(), daemon=True).start()
        return True

    monkeypatch.setattr("webbrowser.open", browser)
    code, out, err = run_cli(capsys, "accounts", "connect", "youtube", "--lang", "hi")
    assert code == 0, err
    assert "https://accounts.google.com/o/oauth2/v2/auth?" in out
    assert f"YouTube (Hindi) connected: {CHANNEL['hi'][1]}" in out
    assert secrets.read_secret("youtube_hi")["channel_id"] == CHANNEL["hi"][0]
    assert load_settings().publish.hi.youtube.channel_id == CHANNEL["hi"][0]
    assert grep_secrets(out, err) == []
