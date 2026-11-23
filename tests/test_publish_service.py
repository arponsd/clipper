"""Step 4 core: service.py (accounts, Google client, OAuth PKCE/state, Meta connect, view, check_live)."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import sys
import urllib.parse

import httpx
import pytest
from publish_fakes import (AUTH_CODE, CHANNEL, CLIENT_ID, CLIENT_SECRET, IG, IG_TOKEN, PAGE, PAGE_TOKEN,
                           REFRESH_TOKEN, FakeNet, client_secret_json, connect_all, connect_meta, connect_youtube,
                           grep_secrets, make_job, meta_error, no_network_client, set_publish, tree_text)

from autoclipper.config import load_settings
from autoclipper.publish import accounts, oauth_google, records, secrets, service

POSIX = sys.platform != "win32"
AS_KEYS = {"connected": bool, "enabled": bool, "account_id": (str, type(None)), "account_name": (str, type(None)),
           "connected_at": (float, type(None)), "problems": list}


@pytest.fixture(autouse=True)
def _clear_pending():
    oauth_google.PENDING.clear()
    yield
    oauth_google.PENDING.clear()


@pytest.fixture
def net() -> FakeNet:
    return FakeNet()


def assert_as(d):
    assert set(d) >= set(AS_KEYS)
    for k, t in AS_KEYS.items():
        assert isinstance(d[k], t), (k, d[k])


# ---- accounts status ---------------------------------------------------------------------------
def test_accounts_status_fresh_install():
    st = service.accounts_status(load_settings())
    assert set(st) == {"dry_run", "google_client", "langs"}
    assert st["dry_run"] is True
    assert st["google_client"] == {"configured": False, "client_id_hint": None}
    assert set(st["langs"]) == {"en", "hi"}
    for lang in ("en", "hi"):
        assert set(st["langs"][lang]) == {"youtube", "facebook", "instagram"}
        for a in st["langs"][lang].values():
            assert set(a) == set(AS_KEYS)
            assert_as(a)
            assert a == {"connected": False, "enabled": False, "account_id": None, "account_name": None,
                         "connected_at": None, "problems": []}


def test_accounts_status_connected_never_contains_secrets():
    connect_all()
    st = service.accounts_status(load_settings())
    yt = st["langs"]["hi"]["youtube"]
    assert yt["connected"] and yt["enabled"] and yt["account_id"] == CHANNEL["hi"][0]
    assert yt["account_name"] == CHANNEL["hi"][1] and yt["problems"] == []
    assert st["langs"]["en"]["instagram"]["account_name"] == IG["en"][1]
    assert st["google_client"]["configured"] is True
    assert st["google_client"]["client_id_hint"] == "12345678…apps.googleusercontent.com"
    text = json.dumps(st, ensure_ascii=False)
    assert grep_secrets(text) == [] and CLIENT_ID not in text


def test_mapping_problems_mismatch_and_same_account():
    connect_all()
    # settings channel differs from the connected one
    service._update_settings(lambda ps: setattr(ps.hi.youtube, "channel_id", "UC" + "Z" * 22))
    a = service.accounts_status(load_settings())["langs"]["hi"]["youtube"]
    assert "settings channel differs from the connected one" in a["problems"]
    # en and hi on the same Page
    connect_meta("hi")
    service._update_settings(lambda ps: setattr(ps.en.facebook, "page_id", PAGE["hi"][0]))
    st = service.accounts_status(load_settings())
    assert any("same Page as hi" in p for p in st["langs"]["en"]["facebook"]["problems"])
    assert any("same Page as en" in p for p in st["langs"]["hi"]["facebook"]["problems"])
    set_publish(allow_same_account=True)
    st = service.accounts_status(load_settings())
    assert not any("same" in p for p in st["langs"]["hi"]["facebook"]["problems"])


# ---- google client -----------------------------------------------------------------------------
def test_set_google_client_happy_and_mode():
    out = service.set_google_client(client_secret_json())
    assert out == {"configured": True, "client_id_hint": "12345678…apps.googleusercontent.com"}
    data = secrets.read_secret("google_client")
    assert data == {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "project_id": "fake-project"}
    if POSIX:
        assert stat.S_IMODE(os.stat(secrets.secret_path("google_client")).st_mode) == 0o600
    # also accepts the raw file text
    assert service.set_google_client(json.dumps(client_secret_json()))["configured"] is True
    service.clear_google_client()
    assert secrets.read_secret("google_client") is None
    service.clear_google_client()  # idempotent


@pytest.mark.parametrize("body,key", [
    ([], "body"), ("not json", "body"), (None, "body"), ({}, "installed"),
    (client_secret_json("web"), "installed"),
    ({"installed": {"client_id": "", "client_secret": "x"}}, "installed.client_id"),
    ({"installed": {"client_id": "a", "client_secret": 5}}, "installed.client_secret"),
    ({"installed": {"client_id": "a b", "client_secret": "x"}}, "installed.client_id"),
    ({"installed": {"client_id": "a", "client_secret": "x" * 2000}}, "installed.client_secret"),
    ({"installed": {"client_id": "a", "client_secret": "b"}, "pad": "x" * 70000}, "body"),
    ("x" * 70000, "body"),
])
def test_set_google_client_validation(body, key):
    with pytest.raises(service.PublishInputError) as ei:
        service.set_google_client(body)
    assert key in ei.value.errors
    assert secrets.read_secret("google_client") is None
    if body == client_secret_json("web"):
        assert "Desktop app" in ei.value.errors["installed"][0]


# ---- OAuth -------------------------------------------------------------------------------------
def test_pkce_challenge_math():
    v = oauth_google.make_verifier()
    assert 43 <= len(v) <= 128 and set(v) <= set(oauth_google._UNRESERVED)
    expect = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    assert oauth_google.challenge(v) == expect
    # RFC 7636 appendix B test vector
    assert oauth_google.challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == \
        "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    with pytest.raises(ValueError):
        oauth_google.make_verifier(42)


def test_begin_connect_needs_client_and_loopback():
    with pytest.raises(service.PublishConflict) as ei:
        service.begin_youtube_connect("hi", "http://127.0.0.1:8765/oauth/google/callback")
    assert "client" in ei.value.message
    service.set_google_client(client_secret_json())
    for bad in ("https://127.0.0.1/cb", "http://example.com/cb", "http://127.0.0.1.evil.com/cb",
                "http://user@127.0.0.1/cb", "http://127.0.0.1/cb?x=1", 5):
        with pytest.raises(service.PublishConflict):
            service.begin_youtube_connect("hi", bad)
    with pytest.raises(service.PublishInputError):
        service.begin_youtube_connect("fr", "http://127.0.0.1:8765/cb")


def test_full_connect_flow(net):
    service.set_google_client(client_secret_json())
    redirect = "http://127.0.0.1:8765/oauth/google/callback"
    b = service.begin_youtube_connect("hi", redirect)
    assert set(b) == {"auth_url", "state", "expires_in", "redirect_uri"}
    assert b["expires_in"] == 600 and b["redirect_uri"] == redirect
    u = urllib.parse.urlsplit(b["auth_url"])
    q = dict(urllib.parse.parse_qsl(u.query))
    assert f"{u.scheme}://{u.netloc}{u.path}" == "https://accounts.google.com/o/oauth2/v2/auth"
    assert q["client_id"] == CLIENT_ID and q["redirect_uri"] == redirect and q["response_type"] == "code"
    assert q["scope"].split() == list(oauth_google.SCOPES)
    assert q["code_challenge_method"] == "S256" and q["access_type"] == "offline" and "consent" in q["prompt"]
    assert q["state"] == b["state"] and len(q["code_challenge"]) == 43
    assert CLIENT_SECRET not in b["auth_url"]
    out = service.complete_youtube_connect(b["state"], AUTH_CODE, None, http=net.client())
    assert out["connected"] is True and out["account_id"] == CHANNEL["hi"][0] and out["lang"] == "hi"
    assert_as(out)
    tok = net.google.requests[0]
    assert tok.form["grant_type"] == "authorization_code" and tok.form["redirect_uri"] == redirect
    assert oauth_google.challenge(tok.form["code_verifier"]) == q["code_challenge"]
    assert net.google.requests[1].url.startswith("https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true")
    data = secrets.read_secret("youtube_hi")
    assert data["refresh_token"] == REFRESH_TOKEN["hi"] and data["channel_id"] == CHANNEL["hi"][0]
    assert len(data["scopes"]) == 2
    assert load_settings().publish.hi.youtube.channel_id == CHANNEL["hi"][0]
    # state is single use
    with pytest.raises(service.OAuthStateError):
        service.complete_youtube_connect(b["state"], AUTH_CODE, None, http=net.client())
    assert grep_secrets(tree_text(), json.dumps(out, ensure_ascii=False)) == []


def test_connect_does_not_overwrite_existing_channel_mapping(net):
    service.set_google_client(client_secret_json())
    service._update_settings(lambda ps: setattr(ps.hi.youtube, "channel_id", "UC" + "Q" * 22))
    b = service.begin_youtube_connect("hi", "http://127.0.0.1:8765/cb")
    out = service.complete_youtube_connect(b["state"], AUTH_CODE, None, http=net.client())
    assert load_settings().publish.hi.youtube.channel_id == "UC" + "Q" * 22
    assert "settings channel differs from the connected one" in out["problems"]


def test_connect_errors(net):
    service.set_google_client(client_secret_json())
    with pytest.raises(service.OAuthStateError):
        service.complete_youtube_connect("nope", AUTH_CODE, None, http=no_network_client())
    with pytest.raises(service.OAuthStateError):
        service.complete_youtube_connect(None, AUTH_CODE, None, http=no_network_client())  # type: ignore[arg-type]
    b = service.begin_youtube_connect("en", "http://127.0.0.1:8765/cb")
    with pytest.raises(service.OAuthStateError) as ei:
        service.complete_youtube_connect(b["state"], None, "access_denied", http=no_network_client())
    assert "declined" in ei.value.message
    b = service.begin_youtube_connect("en", "http://127.0.0.1:8765/cb")
    with pytest.raises(service.OAuthStateError):
        service.complete_youtube_connect(b["state"], "4/wrong-code", None, http=net.client())
    b = service.begin_youtube_connect("en", "http://127.0.0.1:8765/cb")
    net.google.inject["token_authorization_code"] = [httpx.ConnectError("down")] * 3
    with pytest.raises(service.PublishUpstreamError):
        service.complete_youtube_connect(b["state"], AUTH_CODE, None, http=net.client())
    b = service.begin_youtube_connect("en", "http://127.0.0.1:8765/cb")
    net.google.inject["channels"] = [httpx.Response(200, json={"items": []})]
    with pytest.raises(service.OAuthStateError) as ei:
        service.complete_youtube_connect(b["state"], AUTH_CODE, None, http=net.client())
    assert "no YouTube channel" in ei.value.message
    b = service.begin_youtube_connect("en", "http://127.0.0.1:8765/cb")
    net.google.code_refresh_token = None  # type: ignore[assignment]
    with pytest.raises(service.OAuthStateError) as ei:
        service.complete_youtube_connect(b["state"], AUTH_CODE, None, http=net.client())
    assert "refresh token" in ei.value.message
    assert secrets.read_secret("youtube_en") is None


def test_pending_states_ttl_and_cap():
    now = [1000.0]
    ps = oauth_google.PendingStates(ttl=600, max_items=3, clock=lambda: now[0])
    s1 = ps.add("en", "http://127.0.0.1/", "v" * 43)
    now[0] += 601
    assert ps.pop(s1) is None  # expired
    states = [ps.add("en", "http://127.0.0.1/", "v" * 43) for _ in range(5)]
    assert len(ps) == 3
    assert ps.pop(states[0]) is None and ps.pop(states[-1]) is not None
    assert ps.pop(states[-1]) is None  # single use
    assert "v" * 43 not in repr(ps._items)


def test_revoke_and_disconnect_youtube(net):
    connect_youtube("hi")
    service.disconnect("hi", "youtube", http=net.client())
    assert secrets.read_secret("youtube_hi") is None
    assert net.google.requests[-1].url == "https://oauth2.googleapis.com/revoke"
    assert net.google.requests[-1].form == {"token": REFRESH_TOKEN["hi"]}
    # settings id kept, enabled untouched
    assert load_settings().publish.hi.youtube.channel_id == CHANNEL["hi"][0]
    service.disconnect("hi", "youtube", http=no_network_client())  # idempotent, nothing to revoke
    connect_youtube("en")
    service.disconnect("en", "youtube", revoke=False, http=no_network_client())
    assert secrets.read_secret("youtube_en") is None


def test_disconnect_meta_variants():
    connect_meta("hi")
    service.disconnect("hi", "instagram")
    d = secrets.read_secret("meta_hi")
    assert d["page_token"] and d["ig_user_id"] is None
    assert accounts.instagram_account("hi") is None and accounts.facebook_account("hi") is not None
    service.disconnect("hi", "facebook")  # nothing else left -> file removed
    assert secrets.read_secret("meta_hi") is None
    connect_meta("hi", separate_ig_token=True)
    service.disconnect("hi", "facebook")
    assert accounts.facebook_account("hi") is None and accounts.instagram_account("hi") is not None
    assert PAGE_TOKEN["hi"] not in json.dumps(secrets.read_secret("meta_hi"))
    service.disconnect("hi", "meta")
    assert secrets.read_secret("meta_hi") is None
    service.disconnect("hi", "meta")
    with pytest.raises(service.PublishInputError):
        service.disconnect("hi", "tiktok")
    with pytest.raises(service.PublishInputError):
        service.disconnect("xx", "meta")


# ---- Meta connect ------------------------------------------------------------------------------
def test_connect_meta_happy(net):
    out = service.connect_meta("hi", page_id=PAGE["hi"][0], page_token=PAGE_TOKEN["hi"], ig_user_id=IG["hi"][0],
                               ig_token=None, http=net.client())
    assert set(out) == {"facebook", "instagram"}
    assert out["facebook"]["account_name"] == PAGE["hi"][1] and out["instagram"]["account_name"] == IG["hi"][1]
    for a in out.values():
        assert_as(a)
        assert a["connected"] is True
    r = net.graph.requests[0]
    assert r.url == (f"https://graph.facebook.com/v27.0/{PAGE['hi'][0]}?fields=id%2Cname%2Cinstagram_business_account")
    assert r.headers["authorization"] == f"Bearer {PAGE_TOKEN['hi']}"
    d = secrets.read_secret("meta_hi")
    assert d["page_token"] == PAGE_TOKEN["hi"] and d["ig_token"] is None
    if POSIX:
        assert stat.S_IMODE(os.stat(secrets.secret_path("meta_hi")).st_mode) == 0o600
    ps = load_settings().publish
    assert ps.hi.facebook.page_id == PAGE["hi"][0] and ps.hi.instagram.ig_user_id == IG["hi"][0]
    assert grep_secrets(json.dumps(out, ensure_ascii=False), tree_text()) == []


def test_connect_meta_facebook_only_and_separate_ig_token(net):
    out = service.connect_meta("en", page_id=PAGE["en"][0], page_token=PAGE_TOKEN["en"], ig_user_id=None,
                               ig_token=None, http=net.client())
    assert out["facebook"]["connected"] and not out["instagram"]["connected"]
    out = service.connect_meta("en", page_id=PAGE["en"][0], page_token=PAGE_TOKEN["en"], ig_user_id=IG["en"][0],
                               ig_token=IG_TOKEN["en"], http=net.client())
    assert out["instagram"]["connected"]
    assert net.graph.requests[-1].headers["authorization"] == f"Bearer {IG_TOKEN['en']}"


@pytest.mark.parametrize("kw,key", [
    (dict(page_id="12a"), "page_id"), (dict(page_id=""), "page_id"), (dict(page_id="1" * 33), "page_id"),
    (dict(page_id=5), "page_id"),
    (dict(page_token="short"), "page_token"), (dict(page_token="x" * 1025), "page_token"),
    (dict(page_token="has space in the token 123"), "page_token"), (dict(page_token=None), "page_token"),
    (dict(ig_user_id="abc"), "ig_user_id"), (dict(ig_user_id=""), "ig_user_id"),
    (dict(ig_token="short", ig_user_id=IG["hi"][0]), "ig_token"),
    (dict(ig_token=IG_TOKEN["hi"]), "ig_user_id"),
])
def test_connect_meta_validation(kw, key):
    args = dict(page_id=PAGE["hi"][0], page_token=PAGE_TOKEN["hi"], ig_user_id=None, ig_token=None)
    args.update(kw)
    with pytest.raises(service.PublishInputError) as ei:
        service.connect_meta("hi", **args, http=no_network_client())
    assert key in ei.value.errors
    assert secrets.read_secret("meta_hi") is None


def test_connect_meta_rejections(net):
    net.graph.inject["page"] = [meta_error(190, "Error validating access token")]
    with pytest.raises(service.PublishInputError) as ei:
        service.connect_meta("hi", page_id=PAGE["hi"][0], page_token=PAGE_TOKEN["hi"], ig_user_id=None,
                             ig_token=None, http=net.client())
    assert ei.value.errors == {"page_token": ["rejected by Facebook (code 190)"]}
    with pytest.raises(service.PublishInputError) as ei:
        service.connect_meta("hi", page_id=PAGE["hi"][0], page_token=PAGE_TOKEN["hi"], ig_user_id=IG["en"][0],
                             ig_token=None, http=net.client())
    assert ei.value.errors == {"ig_user_id": ["not the Instagram account linked to this Page"]}
    net.graph.inject["page"] = [httpx.ConnectError("down")] * 3
    with pytest.raises(service.PublishUpstreamError):
        service.connect_meta("hi", page_id=PAGE["hi"][0], page_token=PAGE_TOKEN["hi"], ig_user_id=None,
                             ig_token=None, http=net.client())
    net.graph.inject["page"] = [httpx.Response(200, json={"id": "999", "name": "Other"})]
    with pytest.raises(service.PublishInputError) as ei:
        service.connect_meta("hi", page_id=PAGE["hi"][0], page_token=PAGE_TOKEN["hi"], ig_user_id=None,
                             ig_token=None, http=net.client())
    assert "page_id" in ei.value.errors
    assert secrets.read_secret("meta_hi") is None


# ---- view / dry run / check_live ---------------------------------------------------------------
VIEW_PLATFORM_KEYS = {"enabled", "connected", "account", "plan", "status", "published_render_outdated", "problems"}


def test_publish_view_shape_before_and_after():
    job_id = make_job()
    v = service.publish_view(job_id, 1, "hi", load_settings(), active=None)
    assert set(v) == {"dry_run", "final", "final_sha256", "task", "platforms"}
    assert v["dry_run"] is True and v["final"] is True and len(v["final_sha256"]) == 64 and v["task"] is None
    for p, d in v["platforms"].items():
        assert set(d) == VIEW_PLATFORM_KEYS
        assert d["plan"] is None and d["status"] is None and d["account"] is None
        assert "not connected" in d["problems"]
    connect_all()
    service.dry_run(job_id, 1, "hi", ["youtube"], load_settings())
    v = service.publish_view(job_id, 1, "hi", load_settings(), active=None)
    assert v["platforms"]["youtube"]["plan"]["platform"] == "youtube"
    assert v["platforms"]["youtube"]["account"] == {"id": CHANNEL["hi"][0], "name": CHANNEL["hi"][1]}
    assert v["platforms"]["youtube"]["problems"] == []


def test_publish_view_interrupted_and_outdated(monkeypatch):
    job_id = make_job()
    connect_all()
    item, _ = service.load_item(job_id, 1, "hi")
    w = records.FileStatusWriter(job_id, 1, "hi", "youtube", {"platform": "youtube", "state": "uploading",
                                                              "video_sha256": item.sha256})
    v = service.publish_view(job_id, 1, "hi", load_settings(), active=None)
    assert v["platforms"]["youtube"]["status"]["state"] == "interrupted"
    active = {"id": "t", "kind": "publish", "state": "running", "platforms": ["youtube"]}
    v = service.publish_view(job_id, 1, "hi", load_settings(), active=active)
    assert v["platforms"]["youtube"]["status"]["state"] == "uploading" and v["task"] == active
    w.update(state="published", remote_id="X", video_sha256="0" * 64)
    v = service.publish_view(job_id, 1, "hi", load_settings(), active=None)
    assert v["platforms"]["youtube"]["published_render_outdated"] is True
    assert set(v["platforms"]["youtube"]["status"]) == set(records.STATUS_KEYS)


def test_not_found_and_no_final():
    job_id = make_job(clips={1: ["en"]})
    s = load_settings()
    for args in [("nope-job", 1, "en"), (job_id, 2, "en"), (job_id, 1, "hi"), ("../etc", 1, "en")]:
        with pytest.raises(service.PublishNotFound):
            service.publish_view(*args, s, active=None)
        with pytest.raises(service.PublishNotFound):
            service.dry_run(*args, ["youtube"], s)
    with pytest.raises(service.PublishInputError):
        service.dry_run(job_id, 1, "xx", ["youtube"], s)
    with pytest.raises(service.PublishInputError):
        service.dry_run(job_id, -1, "en", ["youtube"], s)
    nofinal = make_job("20261123-120000-nofinal", with_final=False)
    with pytest.raises(service.PublishConflict) as ei:
        service.dry_run(nofinal, 1, "en", ["youtube"], s)
    assert ei.value.message == "clip has no final render yet"
    v = service.publish_view(nofinal, 1, "en", s, active=None)
    assert v["final"] is False and v["final_sha256"] is None
    assert v["platforms"]["youtube"]["problems"] == ["clip has no final render yet"]


@pytest.mark.parametrize("platforms", [[], ["youtube", "youtube"], ["tiktok"], "youtube",
                                       ["youtube", "facebook", "instagram", "youtube"], None])
def test_platform_list_validation(platforms):
    job_id = make_job()
    with pytest.raises(service.PublishInputError) as ei:
        service.dry_run(job_id, 1, "hi", platforms, load_settings())  # type: ignore[arg-type]
    assert "platforms" in ei.value.errors


def test_metadata_missing_falls_back_to_script():
    job_id = make_job(with_metadata=False)
    connect_all()
    plan = service.dry_run(job_id, 1, "hi", ["facebook"], load_settings())["plans"]["facebook"]
    assert "metadata.json missing - re-render" in plan["problems"]
    assert plan["metadata"]["title"].startswith("वो चार")


def test_check_live_conflicts():
    job_id = make_job()
    connect_all(dry_run=True)
    with pytest.raises(service.PublishConflict) as ei:
        service.check_live(job_id, 1, "hi", ["youtube"], load_settings(), force=False)
    assert ei.value.message == "cannot publish"
    assert ei.value.errors["youtube"][0].startswith("publishing is in dry-run mode")
    set_publish(dry_run=False)
    assert service.check_live(job_id, 1, "hi", ["youtube", "facebook"], load_settings(), force=False) == \
        (["youtube", "facebook"], {})
    service._update_settings(lambda ps: setattr(ps.hi.instagram, "enabled", False))
    with pytest.raises(service.PublishConflict) as ei:
        service.check_live(job_id, 1, "hi", ["youtube", "instagram"], load_settings(), force=False)
    assert set(ei.value.errors) == {"instagram"}
    assert "Instagram is disabled for Hindi in settings" in ei.value.errors["instagram"]
    service.disconnect("hi", "youtube", revoke=False)
    with pytest.raises(service.PublishConflict) as ei:
        service.check_live(job_id, 1, "hi", ["youtube"], load_settings(), force=False)
    assert "not connected" in ei.value.errors["youtube"]
    # same account for en and hi
    connect_youtube("hi")
    service._update_settings(lambda ps: setattr(ps.en.youtube, "channel_id", CHANNEL["hi"][0]))
    with pytest.raises(service.PublishConflict) as ei:
        service.check_live(job_id, 1, "hi", ["youtube"], load_settings(), force=False)
    assert any("same channel as en" in p for p in ei.value.errors["youtube"])
