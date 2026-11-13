# -*- coding: utf-8 -*-
"""Write endpoints must not be remotely reachable without an explicit token."""
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.controllers.v1 import auth


def request_from(host):
    return Request({
        "type": "http", "http_version": "1.1", "method": "POST",
        "scheme": "http", "path": "/", "raw_path": b"/",
        "query_string": b"", "root_path": "", "headers": [],
        "client": (host, 12345), "server": ("localhost", 8000),
    })


def test_unconfigured_token_allows_loopback_only(monkeypatch):
    monkeypatch.setattr(auth, "configured_token", lambda: "")
    auth.require_token(request_from("127.0.0.1"))
    auth.require_token(request_from("::1"))
    with pytest.raises(HTTPException) as exc:
        auth.require_token(request_from("192.0.2.10"))
    assert exc.value.status_code == 401


def test_configured_token_is_required_even_on_loopback(monkeypatch):
    monkeypatch.setattr(auth, "configured_token", lambda: "secret-token")
    with pytest.raises(HTTPException) as exc:
        auth.require_token(request_from("127.0.0.1"))
    assert exc.value.status_code == 401
    auth.require_token(request_from("127.0.0.1"), "secret-token")


@pytest.mark.parametrize("host,expected", [
    ("127.0.0.1", True), ("localhost", True), ("::1", True),
    ("::ffff:127.0.0.1", True), ("0.0.0.0", False),
    ("::", False), ("", False), ("192.0.2.1", False), ("example.invalid", False),
])
def test_loopback_host_classification(host, expected):
    assert auth.is_loopback_host(host) is expected
