"""Network blocking shared by the social_common and social_discovery tests.

Every test in those folders runs with this active: any attempt to open a
socket, resolve a host name or call urlopen fails the test immediately.
"""
from __future__ import annotations

import http.client
import socket
import urllib.request


class NetworkBlocked(AssertionError):
    pass


def _blocked(*args, **kwargs):
    raise NetworkBlocked("Network access attempted during an offline test.")


def block_network(monkeypatch) -> None:
    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(socket.socket, "sendto", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)
    monkeypatch.setattr(http.client.HTTPConnection, "connect", _blocked)
    monkeypatch.setattr(urllib.request, "urlopen", _blocked)
