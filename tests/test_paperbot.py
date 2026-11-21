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
    assert t == f"Title\n{LINK}\nHello world{paperbot.POST_TAIL}"


def test_post_includes_authors_line():
    t = paperbot.build_post_text("Title", LINK, "Abstract: Hello.", author="Ada Lovelace, Alan Turing, Grace Hopper")
    assert t == f"Title\n{LINK}\nAda Lovelace et al.\nHello.{paperbot.POST_TAIL}"


def test_authors_dropped_before_abstract_when_title_is_long():
    t = paperbot.build_post_text("T" * 230, LINK, "Abstract: " + "word " * 50, author="Ada Lovelace")
    assert len(t) <= paperbot.MAX_POST_LEN
    assert "Ada Lovelace" not in t
    assert link_facet_text(t) == [LINK]


@pytest.mark.parametrize("author, expected", [
    ("", ""),
    ("Ada Lovelace", "Ada Lovelace"),
    ("Ada Lovelace, Alan Turing", "Ada Lovelace and Alan Turing"),
    ("Ada Lovelace, Alan Turing, Grace Hopper, X Y", "Ada Lovelace et al."),
])
def test_format_authors(author, expected):
    assert paperbot.format_authors(author) == expected


# --- abstract cleaning and truncation --------------------------------------
def test_clean_abstract_decodes_entities_and_collapses_whitespace():
    d = "arXiv:1v1 Announce Type: new \nAbstract: Cats &amp; dogs\n  are  &lt;3 \n friends"
    assert paperbot.clean_abstract(d) == "Cats & dogs are <3 friends"


def test_clean_abstract_keeps_text_after_a_second_abstract_word():
    assert paperbot.clean_abstract("Abstract: We study Abstract: algebras") == "We study Abstract: algebras"


def test_truncate_returns_fitting_text_unchanged_without_ellipsis():
    assert paperbot.truncate_text("Short text.", 50) == "Short text."


def test_truncate_prefers_ending_on_a_full_sentence():
    text = "First sentence is here today. Second sentence goes on and on and on."
    assert paperbot.truncate_text(text, 45) == "First sentence is here today."


def test_truncate_never_cuts_inside_a_word_and_adds_ellipsis():
    out = paperbot.truncate_text("alpha beta gamma delta epsilon zeta", 22)
    assert out.endswith("…") and len(out) <= 22
    assert out[:-1].split(" ")[-1] in {"alpha", "beta", "gamma", "delta", "epsilon", "zeta"}


def test_truncate_does_not_end_on_a_dangling_word():
    # the plain word cut would give "... prominent and"; "and" is dropped too
    out = paperbot.truncate_text("Candidates are not equally prominent and many other words follow", 46)
    assert out == "Candidates are not equally prominent…"


def test_truncate_does_not_leave_an_open_latex_formula():
    text = "We show that the energy $E = mc^2 + \\alpha x_i$ scales with network size in all regimes studied here"
    for room in range(20, len(text)):
        assert paperbot.truncate_text(text, room).count("$") % 2 == 0, room


def test_abbreviations_are_not_treated_as_sentence_ends():
    text = "We compare ours vs. baselines on many tasks, e.g. Twitter data and more words here"
    assert not paperbot.truncate_text(text, 40).endswith("vs.")


def test_url_inside_abstract_is_never_cut_in_half():
    url = "https://github.com/example/repository-with-a-long-name"
    abstract = paperbot.clean_abstract(f"Abstract: Code is at {url} for reuse today. More text follows here.")
    for room in range(20, len(abstract)):
        snippet = paperbot.truncate_text(abstract, room)
        urls_in_snippet = [w for w in snippet.replace("…", " ").split() if w.startswith("http")]
        assert urls_in_snippet in ([], [url]), (room, snippet)


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
    with mock.patch("paperbot.requests.request", return_value=fake_response(xml)):
        feed = paperbot.get_arxiv_feed()
    assert feed[LINK]["title"] == "Scaling in 2.5D networks vs. random graphs"
    assert feed[LINK]["description"] == "Abstract: x"


def test_empty_valid_feed_is_ok():
    with mock.patch("paperbot.requests.request", return_value=fake_response(rss())):
        assert paperbot.get_arxiv_feed() == {}


def test_garbage_response_raises_instead_of_silently_posting_nothing():
    bad = fake_response(b"<html><body>502 Bad Gateway")
    with mock.patch("paperbot.requests.request", return_value=bad), pytest.raises(RuntimeError):
        paperbot.get_arxiv_feed()


def test_http_error_propagates():
    resp = fake_response(b"")
    resp.raise_for_status.side_effect = requests.HTTPError("503")
    with mock.patch("paperbot.requests.request", return_value=resp), pytest.raises(requests.HTTPError):
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


def test_main_skips_revisions_of_older_papers_and_does_not_archive_them(bot, monkeypatch):
    feed = entries(3)
    feed["https://arxiv.org/abs/new0"]["announce_type"] = "new"
    feed["https://arxiv.org/abs/new1"]["announce_type"] = "replace"
    feed["https://arxiv.org/abs/new2"]["announce_type"] = "replace-cross"
    monkeypatch.setattr(paperbot, "get_arxiv_feed", lambda *a, **k: feed)
    posted = []
    monkeypatch.setattr(paperbot, "create_post", lambda text, session, *a, **k: posted.append(text))
    assert paperbot.main() == 0
    assert len(posted) == 1
    assert set(json.loads((bot / "combined.json").read_text())) == {"https://arxiv.org/abs/new0"}


def test_main_archives_only_the_stored_fields_and_passes_authors_to_the_post(bot, monkeypatch):
    feed = entries(1)
    only = feed["https://arxiv.org/abs/new0"]
    only.update(author="Ada Lovelace, Alan Turing", announce_type="cross")
    monkeypatch.setattr(paperbot, "get_arxiv_feed", lambda *a, **k: feed)
    posted = []
    monkeypatch.setattr(paperbot, "create_post", lambda text, session, *a, **k: posted.append(text))
    paperbot.main()
    assert "Ada Lovelace and Alan Turing" in posted[0]
    archived = json.loads((bot / "combined.json").read_text())["https://arxiv.org/abs/new0"]
    assert set(archived) == {"title", "link", "description"}


def test_create_post_sends_language_and_link_facet():
    resp = mock.Mock(ok=True)
    resp.json.return_value = {"uri": "at://did:x/app.bsky.feed.post/1"}
    with mock.patch("paperbot.requests.request", return_value=resp) as post:
        paperbot.create_post(f"Title\n{LINK}\nabstract", {"accessJwt": "j", "did": "did:x"})
    record = post.call_args.kwargs["json"]["record"]
    assert record["langs"] == ["en"]
    assert record["facets"][0]["features"][0]["uri"] == LINK
    assert len(record["text"]) <= paperbot.MAX_POST_LEN


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


# --- retry with backoff ----------------------------------------------------
def status_response(code: int, headers=None) -> mock.Mock:
    r = mock.Mock(status_code=code, headers=headers or {}, ok=code < 400, text=f"error body {code}")
    r.raise_for_status.side_effect = requests.HTTPError(str(code)) if code >= 400 else None
    return r


@pytest.fixture
def sleeps(monkeypatch):
    recorded: list = []
    monkeypatch.setattr(paperbot.time, "sleep", recorded.append)
    return recorded


def test_retry_recovers_from_a_transient_503(sleeps):
    with mock.patch("paperbot.requests.request", side_effect=[status_response(503), status_response(200)]) as req:
        resp = paperbot.request_with_retry("GET", "https://x")
    assert resp.status_code == 200 and req.call_count == 2
    assert sleeps == [paperbot.RETRY_WAITS[0]]


def test_retry_gives_up_after_the_last_attempt_and_returns_the_failing_response(sleeps):
    n = len(paperbot.RETRY_WAITS) + 1
    with mock.patch("paperbot.requests.request", side_effect=[status_response(503)] * n) as req:
        resp = paperbot.request_with_retry("GET", "https://x")
    assert resp.status_code == 503 and req.call_count == n
    assert sleeps == list(paperbot.RETRY_WAITS)


def test_retry_raises_the_last_exception_when_every_attempt_times_out(sleeps):
    n = len(paperbot.RETRY_WAITS) + 1
    with mock.patch("paperbot.requests.request", side_effect=[requests.ReadTimeout("t")] * n), \
            pytest.raises(requests.ReadTimeout):
        paperbot.request_with_retry("GET", "https://x")
    assert len(sleeps) == len(paperbot.RETRY_WAITS)


def test_retry_does_not_retry_a_client_error(sleeps):
    with mock.patch("paperbot.requests.request", return_value=status_response(401)) as req:
        assert paperbot.request_with_retry("POST", "https://x").status_code == 401
    assert req.call_count == 1 and sleeps == []


def test_retry_honours_retry_after_but_caps_it(sleeps):
    slow = status_response(429, {"Retry-After": "9999"})
    short = status_response(429, {"Retry-After": "7"})
    with mock.patch("paperbot.requests.request", side_effect=[slow, short, status_response(200)]):
        paperbot.request_with_retry("GET", "https://x")
    assert sleeps == [paperbot.MAX_RETRY_WAIT, 7]


def test_retry_honours_bluesky_ratelimit_reset_epoch(sleeps, monkeypatch):
    monkeypatch.setattr(paperbot.time, "time", lambda: 1000.0)
    limited = status_response(429, {"ratelimit-reset": "1042"})
    with mock.patch("paperbot.requests.request", side_effect=[limited, status_response(200)]):
        paperbot.request_with_retry("GET", "https://x")
    assert sleeps == [42]


# --- createRecord must never be repeated when the post may already exist ----
@pytest.mark.parametrize("code", [500, 502])
def test_create_post_does_not_retry_after_a_server_error(code, sleeps):
    with mock.patch("paperbot.requests.request", return_value=status_response(code)) as req, \
            pytest.raises(requests.HTTPError):
        paperbot.create_post("hi", {"accessJwt": "j", "did": "d"})
    assert req.call_count == 1 and sleeps == []  # a second post could be a duplicate


def test_create_post_does_not_retry_after_a_read_timeout(sleeps):
    with mock.patch("paperbot.requests.request", side_effect=requests.ReadTimeout("t")) as req, \
            pytest.raises(requests.ReadTimeout):
        paperbot.create_post("hi", {"accessJwt": "j", "did": "d"})
    assert req.call_count == 1


def test_create_post_retries_when_rate_limited(sleeps):
    ok = status_response(200)
    ok.json.return_value = {"uri": "at://x"}
    with mock.patch("paperbot.requests.request", side_effect=[status_response(429), ok]) as req:
        assert paperbot.create_post("hi", {"accessJwt": "j", "did": "d"}) == "at://x"
    assert req.call_count == 2


# --- link card --------------------------------------------------------------
def test_link_card_matches_the_bluesky_external_embed_schema():
    card = paperbot.build_link_card(LINK, "A title", "arXiv:1v1 Announce Type: new \nAbstract: Some  text &amp; more")
    assert card == {
        "$type": "app.bsky.embed.external",
        "external": {"uri": LINK, "title": "A title", "description": "Some text & more"},
    }
    assert "thumb" not in card["external"]  # optional in the lexicon; arXiv only has a generic logo


def test_link_card_description_is_shortened_without_cutting_words():
    card = paperbot.build_link_card(LINK, "T", "Abstract: " + "word " * 400)
    desc = card["external"]["description"]
    assert len(desc) <= paperbot.CARD_DESCRIPTION_LEN and desc.endswith("…")
    assert set(desc[:-1].split(" ")) == {"word"}


def test_create_post_attaches_the_card():
    ok = status_response(200)
    ok.json.return_value = {"uri": "at://x"}
    card = paperbot.build_link_card(LINK, "T", "Abstract: a")
    with mock.patch("paperbot.requests.request", return_value=ok) as req:
        paperbot.create_post("hi", {"accessJwt": "j", "did": "d"}, card=card)
    assert req.call_args.kwargs["json"]["record"]["embed"] == card


def test_create_post_falls_back_to_no_card_when_bluesky_rejects_it():
    rejected = status_response(400)
    rejected.text = "InvalidRequest: bad embed"
    ok = status_response(200)
    ok.json.return_value = {"uri": "at://x"}
    card = paperbot.build_link_card(LINK, "T", "Abstract: a")
    with mock.patch("paperbot.requests.request", side_effect=[rejected, ok]) as req:
        assert paperbot.create_post("hi", {"accessJwt": "j", "did": "d"}, card=card) == "at://x"
    first, second = (c.kwargs["json"]["record"] for c in req.call_args_list)
    assert "embed" in first and "embed" not in second
    assert second["text"] == first["text"]


def test_create_post_without_card_does_not_retry_a_400():
    rejected = status_response(400)
    rejected.text = "bad"
    with mock.patch("paperbot.requests.request", return_value=rejected) as req, pytest.raises(requests.HTTPError):
        paperbot.create_post("hi", {"accessJwt": "j", "did": "d"})
    assert req.call_count == 1


def test_main_attaches_a_card_unless_disabled(bot, monkeypatch):
    monkeypatch.setattr(paperbot, "get_arxiv_feed", lambda *a, **k: entries(1))
    cards = []
    monkeypatch.setattr(paperbot, "create_post", lambda text, session, *a, card=None, **k: cards.append(card))
    paperbot.main()
    assert cards[0]["external"]["uri"] == "https://arxiv.org/abs/new0"
    (bot / "combined.json").unlink()
    cards.clear()
    monkeypatch.setattr(paperbot, "USE_LINK_CARD", False)
    paperbot.main()
    assert cards == [None]
