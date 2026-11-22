"""Auth module, guard middleware order, page/static routes and the uniform error format."""
from __future__ import annotations

import os
import stat
import sys

import pytest
from fastapi.testclient import TestClient
from test_web_support import BASE, api, assert_error_shape, make_client  # noqa: F401

from autoclipper.config import data_dir
from autoclipper.web import auth
from autoclipper.web.auth import (DEFAULT_ALLOWED_HOSTS, TOKEN_HEADER, check_token, host_allowed,
                                  load_or_create_token, origin_allowed, token_path)
from autoclipper.web.errors import APIError, validation_errors


# ---- token ------------------------------------------------------------------------------------
def test_token_created_once_with_0600(tmp_path):
    t1 = load_or_create_token()
    assert len(t1) >= 32
    p = token_path()
    assert p == data_dir() / "web" / "token"
    assert p.read_text(encoding="utf-8") == t1
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert load_or_create_token() == t1  # stable across runs


@pytest.mark.parametrize("content", ["", "short", "   "])
def test_short_or_empty_token_regenerated(content):
    p = token_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    t = load_or_create_token()
    assert len(t) >= 32 and t != content


def test_check_token():
    assert check_token("abc", "abc")
    assert not check_token("abd", "abc")
    assert not check_token(None, "abc")
    assert not check_token("", "abc")
    assert not check_token("abc", "")
    assert not check_token("ä", "abc")  # non-ascii input never raises


@pytest.mark.parametrize("host,ok", [
    ("127.0.0.1:8765", True), ("localhost", True), ("LOCALHOST:1", True), ("[::1]:8765", True),
    ("[::1]", True), ("::1", True), ("evil.com", False), ("127.0.0.1.evil.com", False),
    ("evil.com:8765", False), (None, False), ("", False), ("[::2]:80", False),
])
def test_host_allowed(host, ok):
    assert host_allowed(host, DEFAULT_ALLOWED_HOSTS) is ok


@pytest.mark.parametrize("origin,ok", [
    (None, True), ("http://127.0.0.1:8765", True), ("https://localhost", True), ("http://[::1]:8765", True),
    ("null", False), ("", False), ("http://evil.com", False), ("file://", False), ("ftp://127.0.0.1", False),
    ("http://127.0.0.1.evil.com", False), ("javascript:alert(1)", False),
])
def test_origin_allowed(origin, ok):
    assert origin_allowed(origin, DEFAULT_ALLOWED_HOSTS) is ok


def test_allowed_hosts_for_and_loopback():
    assert auth.allowed_hosts_for("0.0.0.0") == DEFAULT_ALLOWED_HOSTS
    assert "192.168.1.5" in auth.allowed_hosts_for("192.168.1.5")
    assert auth.is_loopback("127.0.0.1") and auth.is_loopback("::1") and auth.is_loopback("localhost")
    assert not auth.is_loopback("0.0.0.0") and not auth.is_loopback("192.168.1.5")


# ---- guard order on real routes ----------------------------------------------------------------
def test_api_requires_token(api):
    client, _, _ = api
    r = client.get("/api/queue", headers={TOKEN_HEADER: ""})
    assert_error_shape(r, 401, "missing auth token")
    bare = TestClient(client.app, base_url=BASE)
    assert_error_shape(bare.get("/api/queue"), 401, "missing auth token")
    assert_error_shape(bare.get("/api/settings"), 401)
    assert_error_shape(bare.post("/api/jobs", json={}), 401)
    assert_error_shape(client.get("/api/queue", headers={TOKEN_HEADER: "x" * 43}), 403, "invalid auth token")
    assert client.get("/api/queue").status_code == 200


def test_unknown_api_path_needs_token_then_404(api):
    client, _, _ = api
    bare = TestClient(client.app, base_url=BASE)
    assert_error_shape(bare.get("/api/nope"), 401)
    assert_error_shape(client.get("/api/nope"), 404)
    assert_error_shape(client.get("/nope"), 404)


def test_method_not_allowed_is_json(api):
    client, _, _ = api
    assert_error_shape(client.delete("/api/queue"), 405)
    assert_error_shape(client.put("/api/jobs"), 405)


def test_host_guard_runs_first(api):
    client, _, _ = api
    evil = TestClient(client.app, base_url="http://evil.example:8765")
    assert_error_shape(evil.get("/"), 403, "forbidden host")
    assert_error_shape(evil.get("/static/app.js"), 403, "forbidden host")
    # even with the right token, and before the token check (no token -> still 403 host)
    r = evil.get("/api/queue", headers={TOKEN_HEADER: client.app.state.token})
    assert_error_shape(r, 403, "forbidden host")
    assert_error_shape(evil.get("/api/queue"), 403, "forbidden host")


def test_extra_allowed_host():
    client, _, _ = make_client(allowed_hosts={"127.0.0.1", "box.lan"})
    lan = TestClient(client.app, base_url="http://box.lan:8765")
    lan.headers[TOKEN_HEADER] = client.app.state.token
    assert lan.get("/api/queue").status_code == 200


def test_origin_guard_on_writes(api):
    client, _, _ = api
    for method in ("post", "put", "delete"):
        r = getattr(client, method)("/api/settings", headers={"Origin": "http://evil.com"})
        assert_error_shape(r, 403, "cross-origin request rejected")
    r = client.post("/api/jobs", json={"url": "https://x.y/v"}, headers={"Sec-Fetch-Site": "cross-site"})
    assert_error_shape(r, 403, "cross-origin request rejected")
    r = client.post("/api/jobs", json={"url": "https://x.y/v"}, headers={"Origin": "null"})
    assert_error_shape(r, 403, "cross-origin request rejected")
    # cross-origin check comes before the token check
    bare = TestClient(client.app, base_url=BASE)
    assert_error_shape(bare.post("/api/jobs", json={}, headers={"Origin": "http://evil.com"}), 403)
    # GET with a foreign Origin is fine (reads need the token anyway)
    assert client.get("/api/queue", headers={"Origin": "http://evil.com"}).status_code == 200
    # same-origin write passes the guard (and fails validation instead)
    r = client.post("/api/jobs", json={}, headers={"Origin": BASE, "Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 422


def test_query_token_only_for_media(api):
    client, _, _ = api
    bare = TestClient(client.app, base_url=BASE)
    tok = client.app.state.token
    assert_error_shape(bare.get(f"/api/queue?token={tok}"), 401)
    assert_error_shape(bare.get(f"/api/settings?token={tok}"), 401)
    # media path: the query token passes the guard (then 404 because the job does not exist)
    r = bare.get(f"/api/jobs/nojob/clips/0/en/video?token={tok}")
    assert_error_shape(r, 404, "job nojob not found")
    r = bare.get("/api/jobs/nojob/clips/0/en/download?token=wrong")
    assert_error_shape(r, 403)
    assert_error_shape(bare.get("/api/jobs/nojob/clips/0/en/video"), 401)
    # query token not accepted on media writes
    assert_error_shape(bare.post(f"/api/jobs/nojob/clips/0/en/video?token={tok}"), 401)


LOOPBACK_PEER = ("127.0.0.1", 50000)


# ---- page / static ------------------------------------------------------------------------------
def test_index_injects_token_with_security_headers(api, tmp_path):
    client, _, _ = api
    # a real browser on this machine connects from loopback (TestClient's default peer is "testclient")
    bare = TestClient(client.app, base_url=BASE, client=LOOPBACK_PEER)
    r = bare.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert client.app.state.token in r.text
    assert "__AUTOCLIPPER_TOKEN__" not in r.text
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["x-frame-options"] == "DENY"
    csp = r.headers["content-security-policy"]
    for part in ("default-src 'self'", "script-src 'self'", "style-src 'self'", "frame-ancestors 'none'",
                 "media-src 'self' blob:", "connect-src 'self'"):
        assert part in csp


def test_index_with_custom_static_dir(tmp_path):
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text(
        '<meta name="autoclipper-token" content="__AUTOCLIPPER_TOKEN__"><p>हिंदी</p>', encoding="utf-8")
    (static / "app.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    client, _, _ = make_client(static_dir=static)
    bare = TestClient(client.app, base_url=BASE, client=LOOPBACK_PEER)
    r = bare.get("/")
    assert f'content="{client.app.state.token}"' in r.text and "हिंदी" in r.text
    r = bare.get("/static/app.js")
    assert r.status_code == 200 and r.text == "console.log(1)"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert_error_shape(bare.get("/static/missing.js"), 404)
    assert_error_shape(bare.get("/static/../secret.txt"), 404)
    assert_error_shape(bare.get("/static/%2e%2e/secret.txt"), 404)
    assert_error_shape(bare.get("/static/"), 404)


def test_index_fallback_when_ui_missing(tmp_path):
    client, _, _ = make_client(static_dir=tmp_path / "nothing")
    r = TestClient(client.app, base_url=BASE, client=LOOPBACK_PEER).get("/")
    assert r.status_code == 200 and client.app.state.token in r.text


def test_favicon_public_204(api):
    client, _, _ = api
    r = TestClient(client.app, base_url=BASE).get("/favicon.ico")
    assert r.status_code == 204


# ---- errors -------------------------------------------------------------------------------------
def test_forced_exception_gives_clean_500(tmp_path):
    def broken_doctor(_settings):
        raise RuntimeError("secret internal detail /home/x/.env")

    client, _, _ = make_client(doctor=broken_doctor)
    c = TestClient(client.app, base_url=BASE, raise_server_exceptions=False)
    c.headers[TOKEN_HEADER] = client.app.state.token
    r = c.get("/api/doctor")
    body = assert_error_shape(r, 500)
    assert body == {"message": "internal error (see server log)", "errors": {}}
    assert "secret" not in r.text and "RuntimeError" not in r.text


def test_malformed_json_and_non_object_bodies_are_422(api):
    client, _, _ = api
    r = client.post("/api/jobs", content=b"{not json", headers={"Content-Type": "application/json"})
    body = assert_error_shape(r, 422, "validation failed")
    assert "body" in body["errors"]
    r = client.post("/api/jobs", json=[1, 2])
    assert_error_shape(r, 422)
    r = client.put("/api/settings", json="string")
    assert_error_shape(r, 422)
    r = client.post("/api/jobs")  # no body at all
    assert_error_shape(r, 422)


def test_validation_never_echoes_input(api):
    client, _, _ = api
    huge = "Z" * 100_000
    r = client.post("/api/jobs", json={"url": huge})
    body = assert_error_shape(r, 422)
    assert huge[:1000] not in r.text
    assert "url" in body["errors"]


def test_validation_errors_helper():
    errs = [{"loc": ("body", "options", "langs", 1), "msg": "bad", "type": "x", "input": "SECRET"},
            {"loc": ("query", "limit"), "msg": "too big", "type": "x"},
            {"loc": ("body", 7), "msg": "JSON decode error", "type": "json_invalid"},
            {"loc": ("body",), "msg": "Field required", "type": "missing"},
            {"loc": (), "msg": "model level", "type": "x"}]
    out = validation_errors(errs)
    assert out["options.langs.1"] == ["bad"]
    assert out["limit"] == ["too big"]
    assert out["body"] == ["JSON decode error", "Field required", "model level"]
    assert "SECRET" not in str(out)


def test_api_error_defaults():
    e = APIError(409, "conflict")
    assert e.status == 409 and e.errors == {} and e.message == "conflict"
