"""Offline tests for paperbot.py: no network, no Bluesky credentials needed."""

import json
import os
import sys
from unittest import mock

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import paperbot  # noqa: E402  (importing must work without BSKYBOT/BSKYPWD)

LINK = "https://arxiv.org/abs/2411.00001"


def rss(*items: tuple) -> bytes:
    body = "".join(
        f"<item><title>{t}</title><link>{l}</link><description>{d}</description></item>"
        for t, l, d in items
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{body}</channel></rss>'.encode()


def fake_response(content: bytes) -> mock.Mock:
    r = mock.Mock()
    r.content = content
    r.raise_for_status.return_value = None
    return r


# --- build_post_text -------------------------------------------------------
def link_facet_text(text: str) -> list:
    b = text.encode()
    return [b[f["index"]["byteStart"]:f["index"]["byteEnd"]].decode() for f in paperbot.parse_facets(text)]


def test_short_post_has_title_link_abstract():
    t = paperbot.build_post_text("Title", LINK, "arXiv:2411.00001v1 Announce Type: new \nAbstract: Hello world")
    assert t == f"Title\n{LINK}\nHello world{paperbot.POST_SUFFIX}"


def test_long_abstract_fits_limit_and_keeps_link_clickable():
    t = paperbot.build_post_text("Title", LINK, "Abstract: " + "word " * 500)
    assert len(t) <= paperbot.MAX_POST_LEN
    assert link_facet_text(t) == [LINK]


def test_very_long_title_is_shortened_but_link_survives():
    # old code sliced the whole string at 293 chars and cut the link in half
    t = paperbot.build_post_text("A" * 400, LINK, "Abstract: text")
    assert len(t) <= paperbot.MAX_POST_LEN
    assert link_facet_text(t) == [LINK]
    assert "…" in t


def test_non_ascii_title_facet_offsets_are_byte_offsets():
    t = paperbot.build_post_text("Réseaux ünïcode ✓", LINK, "Abstract: ok")
    assert link_facet_text(t) == [LINK]


# --- get_arxiv_feed --------------------------------------------------------
def test_title_with_periods_is_not_truncated():
    xml = rss(("Scaling in 2.5D networks vs. random graphs", LINK, "<p>Abstract: x</p>"))
    with mock.patch("paperbot.requests.get", return_value=fake_response(xml)):
        feed = paperbot.get_arxiv_feed()
    assert feed[LINK]["title"] == "Scaling in 2.5D networks vs. random graphs"
    assert feed[LINK]["description"] == "Abstract: x"


def test_empty_valid_feed_is_ok():
    with mock.patch("paperbot.requests.get", return_value=fake_response(rss())):
        assert paperbot.get_arxiv_feed() == {}


def test_garbage_response_raises_instead_of_silently_posting_nothing():
    bad = fake_response(b"<html><body>502 Bad Gateway")
    with mock.patch("paperbot.requests.get", return_value=bad), pytest.raises(RuntimeError):
        paperbot.get_arxiv_feed()


def test_http_error_propagates():
    resp = fake_response(b"")
    resp.raise_for_status.side_effect = requests.HTTPError("503")
    with mock.patch("paperbot.requests.get", return_value=resp), pytest.raises(requests.HTTPError):
        paperbot.get_arxiv_feed()


# --- archive ---------------------------------------------------------------
def test_load_missing_archive_is_empty(tmp_path):
    assert paperbot.load_archive(str(tmp_path / "nope.json")) == {}


def test_save_archive_roundtrip_leaves_no_temp_files(tmp_path):
    f = tmp_path / "a.json"
    paperbot.save_archive({"k": {"title": "t"}}, str(f))
    assert json.loads(f.read_text()) == {"k": {"title": "t"}}
    assert [p.name for p in tmp_path.iterdir()] == ["a.json"]


def test_failed_save_keeps_old_file_intact(tmp_path):
    f = tmp_path / "a.json"
    paperbot.save_archive({"old": {}}, str(f))
    with pytest.raises(TypeError):
        paperbot.save_archive({"bad": object()}, str(f))  # not JSON serialisable
    assert json.loads(f.read_text()) == {"old": {}}
    assert [p.name for p in tmp_path.iterdir()] == ["a.json"]


# --- main ------------------------------------------------------------------
def entries(n: int) -> dict:
    return {
        f"https://arxiv.org/abs/new{i}": {
            "title": f"t{i}", "link": f"https://arxiv.org/abs/new{i}", "description": "Abstract: a",
        }
        for i in range(n)
    }


@pytest.fixture
def bot(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BSKYBOT", "bot.example")
    monkeypatch.setenv("BSKYPWD", "pw")
    monkeypatch.setattr(paperbot.time, "sleep", lambda s: None)
    monkeypatch.setattr(paperbot, "bsky_login_session", lambda *a, **k: {"accessJwt": "j", "did": "did:x"})
    return tmp_path


def test_main_posts_new_entries_and_archives_them(bot, monkeypatch):
    (bot / "combined.json").write_text(json.dumps({"https://arxiv.org/abs/new0": {"title": "already"}}))
    monkeypatch.setattr(paperbot, "get_arxiv_feed", lambda *a, **k: entries(3))
    posted = []
    monkeypatch.setattr(paperbot, "create_post", lambda text, session, *a, **k: posted.append(text))
    assert paperbot.main() == 0
    assert len(posted) == 2  # new0 was already archived
    assert set(json.loads((bot / "combined.json").read_text())) == set(entries(3))


def test_main_failed_post_is_not_archived_and_does_not_block_others(bot, monkeypatch):
    # old code archived everything before posting: one API error lost papers (locally)
    # or caused duplicate posts on the next run (in CI)
    monkeypatch.setattr(paperbot, "get_arxiv_feed", lambda *a, **k: entries(3))
    calls = []

    def flaky(text, session, *a, **k):
        calls.append(text)
        if len(calls) == 2:
            raise requests.HTTPError("500")

    monkeypatch.setattr(paperbot, "create_post", flaky)
    assert paperbot.main() == 1
    assert len(calls) == 3  # third entry was still attempted
    archived = json.loads((bot / "combined.json").read_text())
    assert set(archived) == {"https://arxiv.org/abs/new0", "https://arxiv.org/abs/new2"}


def test_main_with_nothing_new_does_not_log_in(bot, monkeypatch):
    (bot / "combined.json").write_text(json.dumps(entries(2)))
    monkeypatch.setattr(paperbot, "get_arxiv_feed", lambda *a, **k: entries(2))
    login = mock.Mock()
    monkeypatch.setattr(paperbot, "bsky_login_session", login)
    assert paperbot.main() == 0
    login.assert_not_called()


def test_missing_credentials_exit_with_clear_message(monkeypatch):
    monkeypatch.delenv("BSKYBOT", raising=False)
    monkeypatch.delenv("BSKYPWD", raising=False)
    with pytest.raises(SystemExit) as exc:
        paperbot.get_credentials()
    assert "BSKYBOT" in str(exc.value)
