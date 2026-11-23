"""Step 4 web API: publish settings, accounts, Google client, YouTube connect + OAuth callback, Meta connect,
disconnect, and the auth guards on every new route (CONTRACT_STEP4 section 9).

No network: every Google/Meta call goes to tests/publish_fakes.FakeNet (httpx.MockTransport)."""
from __future__ import annotations

import json
import logging
import os
import stat
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from publish_fakes import (ALL_SECRETS, AUTH_CODE, CHANNEL, CLIENT_ID, CLIENT_SECRET, IG, PAGE, PAGE_TOKEN,
                           REFRESH_TOKEN, FakeNet, client_secret_json, connect_all, connect_youtube,
                           fake_client, grep_secrets, tree_text)
from test_web_support import BASE, Stub, assert_error_shape, assert_keys, make_client, read_json

from autoclipper.config import data_dir, settings_path
from autoclipper.web.auth import TOKEN_HEADER
from autoclipper.web.publish_routes import OAuthQueryRedactFilter
from autoclipper.web.worker import Worker

AS_KEYS = {"connected": bool, "enabled": bool, "account_id": (str, type(None)), "account_name": (str, type(None)),
           "connected_at": (float, type(None)), "problems": list}
LANGS = ("en", "hi")
PLATFORMS = ("youtube", "facebook", "instagram")


def secrets_dir():
    return data_dir() / "secrets"


def mode(p) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


def publish_section() -> dict:
    return read_json(settings_path())["publish"]


@pytest.fixture
def net() -> FakeNet:
    return FakeNet()


@pytest.fixture
def papi(net):
    """(client, worker, net): the app with publish_http = the fake network, worker running."""
    stub = Stub()
    worker = Worker(run_job=stub.run_job, rerender=stub.rerender)
    client, worker, _ = make_client(worker=worker, stub=stub, publish_http=net.client)
    with client:
        yield client, worker, net


def assert_accounts(d: dict) -> None:
    assert_keys(d, {"dry_run": bool, "google_client": dict, "langs": dict})
    assert_keys(d["google_client"], {"configured": bool, "client_id_hint": (str, type(None))})
    assert set(d["langs"]) == set(LANGS)
    for lang in LANGS:
        assert set(d["langs"][lang]) == set(PLATFORMS)
        for p in PLATFORMS:
            assert_keys(d["langs"][lang][p], AS_KEYS)
            assert all(isinstance(x, str) for x in d["langs"][lang][p]["problems"])


# =================================================================================================
# publish settings
# =================================================================================================
def test_get_publish_settings_defaults(papi):
    client, _, _ = papi
    r = client.get("/api/publish/settings")
    assert r.status_code == 200
    s = r.json()
    assert s["dry_run"] is True
    assert s["graph_api_version"] == "v27.0"
    for lang in LANGS:
        assert set(s[lang]) == set(PLATFORMS)
        for p in PLATFORMS:
            assert s[lang][p]["enabled"] is False
        assert s[lang]["youtube"]["privacy"] == "private"
        assert s[lang]["youtube"]["channel_id"] == ""
    assert isinstance(s["chunk_mb"], int) and isinstance(s["allow_same_account"], bool)


def test_put_publish_settings_saves_only_publish_section(papi):
    client, _, _ = papi
    before = read_json(settings_path())
    body = client.get("/api/publish/settings").json()
    body["hi"]["youtube"]["privacy"] = "unlisted"
    body["hi"]["youtube"]["enabled"] = True
    body["en"]["instagram"]["share_to_feed"] = False
    body["chunk_mb"] = 16
    r = client.put("/api/publish/settings", json=body)
    assert r.status_code == 200, r.text
    assert r.json() == body
    after = read_json(settings_path())
    assert after["publish"] == body
    assert {k: v for k, v in after.items() if k != "publish"} == {k: v for k, v in before.items() if k != "publish"}
    assert client.get("/api/publish/settings").json() == body
    # the dry-run flag is part of the same object
    body["dry_run"] = False
    assert client.put("/api/publish/settings", json=body).json()["dry_run"] is False
    assert publish_section()["dry_run"] is False


@pytest.mark.parametrize("mutate,key", [
    (lambda b: b.update(bogus=1), "bogus"),
    (lambda b: b["hi"].update(tiktok={}), "hi"),
    (lambda b: b.update(dry_run="no"), "dry_run"),
    (lambda b: b.update(dry_run=0), "dry_run"),
    (lambda b: b["en"]["youtube"].update(privacy="secret"), "en"),
    (lambda b: b["en"]["youtube"].update(channel_id="UC123"), "en"),
    (lambda b: b["en"]["youtube"].update(channel_id="UC" + "a" * 22 + "\n"), "en"),
    (lambda b: b["hi"]["facebook"].update(page_id="12ab"), "hi"),
    (lambda b: b["hi"]["instagram"].update(ig_user_id="1" * 33), "hi"),
    (lambda b: b.update(chunk_mb=0), "chunk_mb"),
    (lambda b: b.update(chunk_mb=257), "chunk_mb"),
    (lambda b: b.update(chunk_mb=-1), "chunk_mb"),
    (lambda b: b.update(chunk_mb=10 ** 30), "chunk_mb"),
    (lambda b: b.update(max_retries=11), "max_retries"),
    (lambda b: b.update(poll_timeout_seconds=1), "poll_timeout_seconds"),
    (lambda b: b.update(graph_api_version="27"), "graph_api_version"),
    (lambda b: b.update(graph_api_version="v" + "9" * 300), "graph_api_version"),
    (lambda b: b["en"]["youtube"].update(category_id=""), "en"),
])
def test_put_publish_settings_422(papi, mutate, key):
    client, _, _ = papi
    before = settings_path().read_bytes()
    body = client.get("/api/publish/settings").json()
    mutate(body)
    body_r = assert_error_shape(client.put("/api/publish/settings", json=body), 422)
    assert any(k == key or k.startswith(key + ".") for k in body_r["errors"]), body_r
    assert settings_path().read_bytes() == before


@pytest.mark.parametrize("raw", [b"[]", b"null", b"\"x\"", b"{", b"", b"1" * 5000])
def test_put_publish_settings_not_an_object(papi, raw):
    client, _, _ = papi
    before = settings_path().read_bytes()
    r = client.put("/api/publish/settings", content=raw, headers={"Content-Type": "application/json"})
    assert_error_shape(r, 422)
    assert settings_path().read_bytes() == before


def test_generic_settings_api_still_carries_publish(papi):
    client, _, _ = papi
    s = client.get("/api/settings").json()
    assert s["publish"]["dry_run"] is True
    s["publish"]["en"]["facebook"]["enabled"] = True
    r = client.put("/api/settings", json=s)
    assert r.status_code == 200, r.text
    assert client.get("/api/publish/settings").json()["en"]["facebook"]["enabled"] is True
    s["publish"]["en"]["facebook"]["nope"] = 1
    assert "publish.en.facebook.nope" in assert_error_shape(client.put("/api/settings", json=s), 422)["errors"]


# =================================================================================================
# accounts
# =================================================================================================
def test_accounts_fresh_install(papi):
    client, _, net = papi
    r = client.get("/api/publish/accounts")
    assert r.status_code == 200
    d = r.json()
    assert_accounts(d)
    assert d["dry_run"] is True
    assert d["google_client"] == {"configured": False, "client_id_hint": None}
    for lang in LANGS:
        for p in PLATFORMS:
            a = d["langs"][lang][p]
            assert a["connected"] is False and a["enabled"] is False and a["account_id"] is None
    assert net.requests == []


def test_accounts_connected_never_returns_secrets(papi):
    client, _, net = papi
    connect_all(dry_run=True)
    r = client.get("/api/publish/accounts")
    d = r.json()
    assert_accounts(d)
    assert d["google_client"]["configured"] is True
    assert d["google_client"]["client_id_hint"].endswith("apps.googleusercontent.com")
    for lang in LANGS:
        assert d["langs"][lang]["youtube"]["account_id"] == CHANNEL[lang][0]
        assert d["langs"][lang]["youtube"]["account_name"] == CHANNEL[lang][1]
        assert d["langs"][lang]["facebook"]["account_id"] == PAGE[lang][0]
        assert d["langs"][lang]["instagram"]["account_id"] == IG[lang][0]
        for p in PLATFORMS:
            assert d["langs"][lang][p]["connected"] is True and d["langs"][lang][p]["enabled"] is True
            assert d["langs"][lang][p]["problems"] == []
    assert grep_secrets(r.text) == []
    assert CLIENT_ID not in r.text  # only a hint
    assert net.requests == []  # status is computed locally


def test_accounts_same_account_problem(papi):
    client, _, _ = papi
    connect_all(dry_run=True)
    body = client.get("/api/publish/settings").json()
    body["en"]["facebook"]["page_id"] = PAGE["hi"][0]  # settings point en at the hi Page
    assert client.put("/api/publish/settings", json=body).status_code == 200
    probs = client.get("/api/publish/accounts").json()["langs"]["en"]["facebook"]["problems"]
    assert probs and all(isinstance(p, str) for p in probs)


# =================================================================================================
# google client
# =================================================================================================
def test_put_google_client_happy(papi):
    client, _, net = papi
    r = client.put("/api/publish/google-client", json=client_secret_json())
    assert r.status_code == 200, r.text
    assert_keys(r.json(), {"configured": bool, "client_id_hint": str})
    assert r.json()["configured"] is True
    assert CLIENT_SECRET not in r.text and CLIENT_ID not in r.text
    p = secrets_dir() / "google_client.json"
    assert p.is_file()
    if os.name == "posix":
        assert mode(p) == 0o600
        assert mode(secrets_dir()) == 0o700
    saved = read_json(p)
    assert saved["client_id"] == CLIENT_ID and saved["client_secret"] == CLIENT_SECRET
    assert client.get("/api/publish/accounts").json()["google_client"]["configured"] is True
    assert grep_secrets(tree_text()) == []
    assert net.requests == []


@pytest.mark.parametrize("body,key", [
    (client_secret_json("web"), None),
    ({"installed": {"client_id": "", "client_secret": "x"}}, None),
    ({"installed": {"client_id": CLIENT_ID}}, None),
    ({"installed": "nope"}, None),
    ({}, None),
    ({"other": 1}, None),
])
def test_put_google_client_422(papi, body, key):
    client, _, _ = papi
    r = client.put("/api/publish/google-client", json=body)
    b = assert_error_shape(r, 422)
    assert b["errors"]
    assert not (secrets_dir() / "google_client.json").exists()
    assert CLIENT_SECRET not in r.text


def test_put_google_client_web_client_says_desktop(papi):
    client, _, _ = papi
    b = assert_error_shape(client.put("/api/publish/google-client", json=client_secret_json("web")), 422)
    assert "Desktop" in json.dumps(b)


@pytest.mark.parametrize("raw", [b"[]", b"\"x\"", b"{", b"", b"\xff\xfe", b"[" * 60000])
def test_put_google_client_bad_json(papi, raw):
    client, _, _ = papi
    assert_error_shape(client.put("/api/publish/google-client", content=raw,
                                  headers={"Content-Type": "application/json"}), 422)
    assert not (secrets_dir() / "google_client.json").exists()


def test_put_google_client_too_large(papi):
    client, _, _ = papi
    big = client_secret_json()
    big["installed"]["padding"] = "x" * (64 * 1024)
    assert_error_shape(client.put("/api/publish/google-client", json=big), 413)
    assert not (secrets_dir() / "google_client.json").exists()


def test_delete_google_client(papi):
    client, _, _ = papi
    client.put("/api/publish/google-client", json=client_secret_json())
    r = client.delete("/api/publish/google-client")
    assert r.status_code == 204 and r.content == b""
    assert not (secrets_dir() / "google_client.json").exists()
    assert client.delete("/api/publish/google-client").status_code == 204  # idempotent
    assert client.get("/api/publish/accounts").json()["google_client"]["configured"] is False


# =================================================================================================
# YouTube connect + OAuth callback
# =================================================================================================
def begin(client, lang="hi", host=None) -> dict:
    headers = {"Host": host} if host else {}
    r = client.post(f"/api/publish/accounts/{lang}/youtube/connect", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_youtube_connect_needs_google_client(papi):
    client, _, _ = papi
    assert_error_shape(client.post("/api/publish/accounts/hi/youtube/connect"), 409)


def test_youtube_connect_returns_auth_url(papi):
    client, _, net = papi
    client.put("/api/publish/google-client", json=client_secret_json())
    d = begin(client, "en")
    assert_keys(d, {"auth_url": str, "state": str, "expires_in": int, "redirect_uri": str})
    assert d["expires_in"] == 600
    assert d["redirect_uri"] == "http://127.0.0.1:8765/oauth/google/callback"
    u = urlsplit(d["auth_url"])
    assert (u.scheme, u.netloc) == ("https", "accounts.google.com")
    q = parse_qs(u.query)
    assert q["client_id"] == [CLIENT_ID]
    assert q["redirect_uri"] == [d["redirect_uri"]]
    assert q["state"] == [d["state"]]
    assert q["code_challenge_method"] == ["S256"]
    assert "youtube.upload" in q["scope"][0]
    assert CLIENT_SECRET not in json.dumps(d)
    assert net.requests == []
    # localhost works too, and the redirect follows the page's address
    d2 = begin(client, "hi", host="localhost:8765")
    assert d2["redirect_uri"] == "http://localhost:8765/oauth/google/callback"
    assert d2["state"] != d["state"]


def test_youtube_connect_422_and_non_loopback_host(net):
    stub = Stub()
    client, _, _ = make_client(worker=Worker(run_job=stub.run_job, rerender=stub.rerender), stub=stub,
                               publish_http=net.client, allowed_hosts={"127.0.0.1", "studio.lan"})
    with client:
        client.put("/api/publish/google-client", json=client_secret_json())
        for bad in ("fr", "EN", "x" * 300):
            assert "lang" in assert_error_shape(client.post(f"/api/publish/accounts/{bad}/youtube/connect"), 422)["errors"]
        r = client.post("/api/publish/accounts/hi/youtube/connect", headers={"Host": "studio.lan:8765"})
        assert_error_shape(r, 409, "127.0.0.1")


def test_oauth_callback_connects_channel(papi, caplog):
    client, _, net = papi
    caplog.set_level(logging.DEBUG)
    client.put("/api/publish/google-client", json=client_secret_json())
    d = begin(client, "hi")
    anon = TestClient(client.app, base_url=BASE)  # no auth token: the one-time state is the guard
    r = anon.get("/oauth/google/callback", params={"state": d["state"], "code": AUTH_CODE, "scope": "x"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert "<script" not in r.text
    assert "YouTube connected" in r.text
    assert CHANNEL["hi"][1] in r.text
    assert grep_secrets(r.text) == []
    p = secrets_dir() / "youtube_hi.json"
    assert p.is_file()
    if os.name == "posix":
        assert mode(p) == 0o600
    assert read_json(p)["refresh_token"] == REFRESH_TOKEN["hi"]
    assert publish_section()["hi"]["youtube"]["channel_id"] == CHANNEL["hi"][0]
    acc = client.get("/api/publish/accounts").json()["langs"]["hi"]["youtube"]
    assert acc["connected"] is True and acc["account_name"] == CHANNEL["hi"][1]
    # the code exchange carried the PKCE verifier and the same redirect_uri; nothing secret in a URL
    token_calls = [x for x in net.requests if x.url.startswith("https://oauth2.googleapis.com/token")]
    assert token_calls and token_calls[0].form["redirect_uri"] == d["redirect_uri"]
    assert token_calls[0].form.get("code_verifier")
    assert all(grep_secrets(x.url) == [] for x in net.requests)
    # single use
    again = anon.get("/oauth/google/callback", params={"state": d["state"], "code": AUTH_CODE})
    assert again.status_code == 400 and "<script" not in again.text
    assert grep_secrets(caplog.text, tree_text()) == []


@pytest.mark.parametrize("params", [
    {},
    {"code": AUTH_CODE},
    {"state": "unknown-state", "code": AUTH_CODE},
    {"state": "x" * 5000, "code": AUTH_CODE},
    {"state": "bad state with spaces", "code": AUTH_CODE},
])
def test_oauth_callback_bad_state_400(papi, params):
    client, _, net = papi
    r = TestClient(client.app, base_url=BASE).get("/oauth/google/callback", params=params)
    assert r.status_code == 400
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["cache-control"] == "no-store"
    assert not (secrets_dir() / "youtube_hi.json").exists()
    assert net.requests == []


def test_oauth_callback_access_denied(papi):
    client, _, net = papi
    client.put("/api/publish/google-client", json=client_secret_json())
    d = begin(client, "hi")
    anon = TestClient(client.app, base_url=BASE)
    r = anon.get("/oauth/google/callback", params={"state": d["state"], "error": "access_denied"})
    assert r.status_code == 400
    assert "declined" in r.text
    assert not (secrets_dir() / "youtube_hi.json").exists()
    assert net.requests == []
    # the state was consumed
    assert anon.get("/oauth/google/callback", params={"state": d["state"], "code": AUTH_CODE}).status_code == 400


def test_oauth_callback_host_guard(papi):
    client, _, _ = papi
    r = TestClient(client.app, base_url="http://evil.example").get("/oauth/google/callback",
                                                                    params={"state": "s", "code": "c"})
    assert_error_shape(r, 403)


def test_oauth_callback_google_unreachable_503(monkeypatch):
    def down(request):
        raise httpx.ConnectError("no route", request=request)

    monkeypatch.setattr("autoclipper.publish.service.time.sleep", lambda s: None)
    stub = Stub()
    client, _, _ = make_client(worker=Worker(run_job=stub.run_job, rerender=stub.rerender), stub=stub,
                               publish_http=lambda: fake_client(down))
    with client:
        client.put("/api/publish/google-client", json=client_secret_json())
        d = begin(client, "en")
        r = TestClient(client.app, base_url=BASE).get("/oauth/google/callback",
                                                      params={"state": d["state"], "code": AUTH_CODE})
        assert r.status_code == 503
        assert "Google" in r.text and "<script" not in r.text
        assert grep_secrets(r.text) == []
        assert not (secrets_dir() / "youtube_en.json").exists()


def test_oauth_access_log_filter_hides_code_and_state():
    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                            ("127.0.0.1:5000", "GET", f"/oauth/google/callback?state=abc&code={AUTH_CODE}", "1.1", 200),
                            None)
    OAuthQueryRedactFilter().filter(rec)
    text = rec.getMessage()
    assert AUTH_CODE not in text and "state=abc" not in text
    assert "code=***" in text and "state=***" in text


# =================================================================================================
# Meta connect
# =================================================================================================
def meta_body(lang="hi", **kw) -> dict:
    b = {"page_id": PAGE[lang][0], "page_token": PAGE_TOKEN[lang], "ig_user_id": IG[lang][0], "ig_token": None}
    b.update(kw)
    return b


def test_meta_connect_happy(papi):
    client, _, net = papi
    r = client.post("/api/publish/accounts/hi/meta/connect", json=meta_body())
    assert r.status_code == 200, r.text
    d = r.json()
    assert set(d) == {"facebook", "instagram"}
    for p in ("facebook", "instagram"):
        assert_keys(d[p], AS_KEYS)
        assert d[p]["connected"] is True
    assert d["facebook"]["account_id"] == PAGE["hi"][0] and d["facebook"]["account_name"] == PAGE["hi"][1]
    assert d["instagram"]["account_id"] == IG["hi"][0]
    assert grep_secrets(r.text) == []
    p = secrets_dir() / "meta_hi.json"
    if os.name == "posix":
        assert mode(p) == 0o600
    assert read_json(p)["page_token"] == PAGE_TOKEN["hi"]
    pub = publish_section()["hi"]
    assert pub["facebook"]["page_id"] == PAGE["hi"][0] and pub["instagram"]["ig_user_id"] == IG["hi"][0]
    assert pub["facebook"]["enabled"] is False  # connecting does not enable
    # tokens went in headers, never in URLs
    assert net.requests and all(grep_secrets(x.url) == [] for x in net.requests)
    assert grep_secrets(tree_text()) == []


def test_meta_connect_facebook_only(papi):
    client, _, _ = papi
    r = client.post("/api/publish/accounts/en/meta/connect", json=meta_body("en", ig_user_id=None))
    assert r.status_code == 200, r.text
    assert r.json()["facebook"]["connected"] is True
    assert r.json()["instagram"]["connected"] is False


@pytest.mark.parametrize("body,key", [
    ({"page_token": PAGE_TOKEN["hi"]}, "page_id"),
    ({"page_id": PAGE["hi"][0]}, "page_token"),
    (meta_body(extra=1), "extra"),
    (meta_body(page_id="abc"), "page_id"),
    (meta_body(page_id=""), "page_id"),
    (meta_body(page_id="1" * 33), "page_id"),
    (meta_body(page_id="123\n"), "page_id"),
    (meta_body(page_id=2220002), "page_id"),
    (meta_body(page_token=""), "page_token"),
    (meta_body(page_token="short"), "page_token"),
    (meta_body(page_token="x" * 1025), "page_token"),
    (meta_body(page_token="EAAF with spaces 0123456789"), "page_token"),
    (meta_body(page_token=None), "page_token"),
    (meta_body(ig_user_id="ig-1"), "ig_user_id"),
    (meta_body(ig_user_id=17841400000000002), "ig_user_id"),
    (meta_body(ig_token="tiny"), "ig_token"),
    (meta_body(ig_token=["x" * 30]), "ig_token"),
])
def test_meta_connect_422(papi, body, key):
    client, _, net = papi
    r = client.post("/api/publish/accounts/hi/meta/connect", json=body)
    b = assert_error_shape(r, 422)
    assert key in b["errors"], b
    assert grep_secrets(r.text) == []
    assert not (secrets_dir() / "meta_hi.json").exists()
    assert net.requests == []


@pytest.mark.parametrize("raw", [b"[]", b"{", b"null", b"1" * 5000])
def test_meta_connect_bad_json(papi, raw):
    client, _, _ = papi
    assert_error_shape(client.post("/api/publish/accounts/hi/meta/connect", content=raw,
                                   headers={"Content-Type": "application/json"}), 422)


def test_meta_connect_token_rejected_by_facebook(papi):
    client, _, _ = papi
    r = client.post("/api/publish/accounts/hi/meta/connect", json=meta_body(page_token="EAAF" + "Z" * 40))
    b = assert_error_shape(r, 422)
    assert "page_token" in b["errors"]
    assert "EAAF" + "Z" * 40 not in r.text
    assert not (secrets_dir() / "meta_hi.json").exists()


def test_meta_connect_wrong_ig_account(papi):
    client, _, _ = papi
    r = client.post("/api/publish/accounts/hi/meta/connect", json=meta_body(ig_user_id=IG["en"][0]))
    b = assert_error_shape(r, 422)
    assert "ig_user_id" in b["errors"]
    assert not (secrets_dir() / "meta_hi.json").exists()


def test_meta_connect_unreachable_503(monkeypatch):
    def down(request):
        raise httpx.ConnectTimeout("timed out", request=request)

    monkeypatch.setattr("autoclipper.publish.service.time.sleep", lambda s: None)
    stub = Stub()
    client, _, _ = make_client(worker=Worker(run_job=stub.run_job, rerender=stub.rerender), stub=stub,
                               publish_http=lambda: fake_client(down))
    with client:
        r = client.post("/api/publish/accounts/hi/meta/connect", json=meta_body())
        assert_error_shape(r, 503)
        assert grep_secrets(r.text) == []
        assert not (secrets_dir() / "meta_hi.json").exists()


# =================================================================================================
# disconnect
# =================================================================================================
def test_disconnect_youtube_revokes_and_removes(papi):
    client, _, net = papi
    connect_all(dry_run=True)
    r = client.delete("/api/publish/accounts/hi/youtube")
    assert r.status_code == 204 and r.content == b""
    assert not (secrets_dir() / "youtube_hi.json").exists()
    assert (secrets_dir() / "youtube_en.json").exists()
    revokes = [x for x in net.requests if "/revoke" in x.url]
    assert len(revokes) == 1 and grep_secrets(revokes[0].url) == []
    pub = publish_section()["hi"]["youtube"]
    assert pub["channel_id"] == CHANNEL["hi"][0] and pub["enabled"] is True  # settings kept
    acc = client.get("/api/publish/accounts").json()["langs"]["hi"]["youtube"]
    assert acc["connected"] is False
    # idempotent
    assert client.delete("/api/publish/accounts/hi/youtube").status_code == 204


def test_disconnect_no_revoke_sends_nothing(papi):
    client, _, net = papi
    connect_youtube("en")
    assert client.delete("/api/publish/accounts/en/youtube", params={"revoke": "false"}).status_code == 204
    assert net.requests == []
    assert not (secrets_dir() / "youtube_en.json").exists()


def test_disconnect_meta_and_instagram(papi):
    client, _, _ = papi
    connect_all(dry_run=True)
    assert client.delete("/api/publish/accounts/en/instagram").status_code == 204
    acc = client.get("/api/publish/accounts").json()["langs"]["en"]
    assert acc["instagram"]["connected"] is False and acc["facebook"]["connected"] is True
    assert client.delete("/api/publish/accounts/en/meta").status_code == 204
    acc = client.get("/api/publish/accounts").json()["langs"]["en"]
    assert acc["facebook"]["connected"] is False
    assert not (secrets_dir() / "meta_en.json").exists()
    assert (secrets_dir() / "meta_hi.json").exists()


@pytest.mark.parametrize("path,key", [
    ("/api/publish/accounts/hi/tiktok", "platform"),
    ("/api/publish/accounts/hi/META", "platform"),
    ("/api/publish/accounts/xx/youtube", "lang"),
    ("/api/publish/accounts/hi/youtube?revoke=maybe", "revoke"),
])
def test_disconnect_422(papi, path, key):
    client, _, _ = papi
    assert key in assert_error_shape(client.delete(path), 422)["errors"]


# =================================================================================================
# auth guards on every new /api route
# =================================================================================================
ROUTES = [
    ("GET", "/api/publish/settings", None),
    ("PUT", "/api/publish/settings", {}),
    ("GET", "/api/publish/accounts", None),
    ("PUT", "/api/publish/google-client", client_secret_json()),
    ("DELETE", "/api/publish/google-client", None),
    ("POST", "/api/publish/accounts/hi/youtube/connect", None),
    ("POST", "/api/publish/accounts/hi/meta/connect", meta_body()),
    ("DELETE", "/api/publish/accounts/hi/youtube", None),
    ("GET", "/api/jobs/20261123-120000-pubtst/clips/1/hi/publish", None),
    ("POST", "/api/jobs/20261123-120000-pubtst/clips/1/hi/publish", {"platforms": ["youtube"]}),
]


@pytest.mark.parametrize("method,path,body", ROUTES)
def test_new_routes_need_token(papi, method, path, body):
    client, _, net = papi
    anon = TestClient(client.app, base_url=BASE)
    assert_error_shape(anon.request(method, path, json=body), 401, "missing auth token")
    bad = TestClient(client.app, base_url=BASE, headers={TOKEN_HEADER: "w" * 43})
    assert_error_shape(bad.request(method, path, json=body), 403, "invalid auth token")
    evil = TestClient(client.app, base_url="http://evil.example", headers={TOKEN_HEADER: client.headers[TOKEN_HEADER]})
    assert_error_shape(evil.request(method, path, json=body), 403, "forbidden host")
    if method != "GET":
        r = client.request(method, path, json=body, headers={"Origin": "https://evil.example"})
        assert_error_shape(r, 403, "cross-origin")
        r = client.request(method, path, json=body, headers={"Sec-Fetch-Site": "cross-site"})
        assert_error_shape(r, 403, "cross-origin")
    assert not secrets_dir().exists() or not any(secrets_dir().iterdir())
    assert net.requests == []


def test_secret_values_never_in_any_response(papi):
    client, _, _ = papi
    client.put("/api/publish/google-client", json=client_secret_json())
    client.post("/api/publish/accounts/hi/meta/connect", json=meta_body())
    texts = [client.get(p).text for p in ("/api/publish/settings", "/api/publish/accounts", "/api/settings",
                                          "/api/settings/schema")]
    assert grep_secrets(*texts) == []
    assert all(s not in "".join(texts) for s in ALL_SECRETS)
