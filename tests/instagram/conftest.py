"""Shared fixtures for the Instagram tests.

Every test in this folder runs with network access blocked: any attempt to
open a socket, resolve a hostname or call urlopen fails the test. All tokens
and secrets below are FAKE placeholders that only exist to test redaction.
"""
from __future__ import annotations

import http.client
import json
import socket
import urllib.request

import pytest

from webcrawler.instagram.client import RawResponse
from webcrawler.instagram.redaction import Redactor, default_redactor

FAKE_FB_TOKEN = "EAAFAKE" + "0123456789abcdefABCDEF" * 2
FAKE_IG_TOKEN = "IGAAFAKE" + "0123456789abcdefABCDEF" * 2
# Fake app secret in the 32-hex-character format Meta uses (not a real secret).
FAKE_SECRET = "0f0f0f0f" * 4


class NetworkBlocked(AssertionError):
    pass


def _blocked(*args, **kwargs):
    raise NetworkBlocked("Network access attempted during an offline test.")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(socket.socket, "sendto", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)
    monkeypatch.setattr(http.client.HTTPConnection, "connect", _blocked)
    monkeypatch.setattr(urllib.request, "urlopen", _blocked)
    yield


@pytest.fixture(autouse=True)
def clean_default_redactor():
    """Keep the process-wide redactor from carrying secrets between tests."""
    default_redactor.clear()
    yield
    default_redactor.clear()


@pytest.fixture
def redactor():
    return Redactor()


def response(status=200, body=None, headers=None, raw: bytes | None = None) -> RawResponse:
    if raw is None:
        raw = json.dumps(body if body is not None else {}).encode("utf-8")
    return RawResponse(status, headers or {}, raw)


class FakeTransport:
    """Returns scripted responses (or raises scripted exceptions) in order and
    records every request it was given. It never touches the network."""

    def __init__(self, *script):
        self.script = list(script)
        self.requests = []
        self.timeouts = []

    def send(self, request, timeout):
        self.requests.append(request)
        self.timeouts.append(timeout)
        if not self.script:
            raise AssertionError("FakeTransport: no scripted response left")
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class SleepRecorder:
    def __init__(self):
        self.calls = []

    def __call__(self, seconds):
        self.calls.append(seconds)
