"""Test safety net: no test may touch the network.

A test that forgets to mock an HTTP call would otherwise reach the real arXiv or
Bluesky servers. Tests that mock ``paperbot.requests.request`` never get this far.
"""

import pytest
import requests


@pytest.fixture(autouse=True)
def block_real_network(monkeypatch):
    def refuse(self, request, **kwargs):
        raise AssertionError(f"test tried a real HTTP request: {request.method} {request.url}")

    monkeypatch.setattr(requests.Session, "send", refuse)
