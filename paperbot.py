"""Bluesky bot: reads an arXiv RSS feed and posts papers it has not posted yet.

State lives in ``combined.json`` (link -> entry). An entry is added to it only
after its post succeeded, and the file is rewritten after every post, so a crash
or API error can neither lose a paper nor cause a double post.
"""

import html
import json
import logging
import os
import random
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Type

import feedparser
import requests

logger = logging.getLogger(__name__)

PDS_URL = "https://bsky.social"
FEED_SUBJECT = "cs.si+physics.soc-ph"
ARCHIVE_FILE = "combined.json"
MAX_POST_LEN = 300  # Bluesky limit, in graphemes (code points here is a safe upper bound)
POST_TAIL = " 📈🤖"  # signature of the bot, always the last thing in a post
POST_LANGS = ["en"]  # arXiv abstracts are English; lets Bluesky language feeds pick the posts up
MIN_ABSTRACT = 40  # below this many free characters the abstract is left out
# arXiv announces revised versions of older papers ("replace", "replace-cross") in the same feed.
# They are not new papers, so they are not posted. Set to frozenset() to post everything again.
SKIP_ANNOUNCE_TYPES = frozenset({"replace", "replace-cross"})
ARCHIVE_FIELDS = ("title", "link", "description")  # what is stored per posted paper
USE_LINK_CARD = True  # attach an app.bsky.embed.external card (title + abstract) to each post
CARD_DESCRIPTION_LEN = 500  # no schema limit exists; keep the card readable
REQUEST_TIMEOUT = 30  # seconds; without it a stalled connection hangs the job
RETRY_WAITS = (5, 20, 60)  # seconds to wait before the 2nd, 3rd and 4th attempt
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})  # transient HTTP statuses
MAX_RETRY_WAIT = 300  # cap for a wait requested by the server
SLEEP_RANGE = (60, 300)  # seconds between posts
SESSION_MAX_AGE = 3000  # seconds; access tokens last ~2h, refresh well before


# %%
def get_credentials() -> Tuple[str, str]:
    """Read bot credentials from the environment.

    Read at call time, not import time, so the module can be imported (e.g. by
    tests) without secrets.

    Returns:
        (handle, password); the password should be a Bluesky app password.
    """
    try:
        return os.environ["BSKYBOT"], os.environ["BSKYPWD"]
    except KeyError as e:
        raise SystemExit(
            f"Missing environment variable {e}. Set BSKYBOT (handle) and BSKYPWD (app password)."
        ) from e


def _server_wait(resp: requests.Response) -> Optional[float]:
    """Seconds the server asks us to wait (Retry-After, or Bluesky's ratelimit-reset epoch)."""
    try:
        if "Retry-After" in resp.headers:
            return float(resp.headers["Retry-After"])
        if "ratelimit-reset" in resp.headers:
            return float(resp.headers["ratelimit-reset"]) - time.time()
    except ValueError:
        pass
    return None


def request_with_retry(
    method: str,
    url: str,
    *,
    retry_status: frozenset = RETRY_STATUS,
    retry_errors: Tuple[Type[Exception], ...] = (requests.ConnectionError, requests.Timeout),
    **kwargs: Any,
) -> requests.Response:
    """``requests.request`` that retries transient failures with a growing wait.

    Retries on the HTTP statuses in ``retry_status`` and on the exceptions in
    ``retry_errors``; a wait requested by the server (429) is honoured, capped
    at MAX_RETRY_WAIT. After the last attempt the final response is returned
    (the caller decides with ``raise_for_status``) or the final exception raised.
    Non-idempotent calls pass narrower ``retry_status`` / ``retry_errors`` so a
    request the server may already have processed is never repeated.
    """
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    waits = list(RETRY_WAITS)
    while True:
        error: Optional[Exception] = None
        delay: Optional[float] = None
        try:
            resp = requests.request(method, url, **kwargs)
        except retry_errors as e:
            error, reason = e, type(e).__name__
        else:
            if resp.status_code not in retry_status:
                return resp
            reason, delay = f"HTTP {resp.status_code}", _server_wait(resp)
        if not waits:
            if error is not None:
                raise error
            return resp
        wait: float = waits.pop(0)
        if delay is not None and delay > 0:
            wait = min(delay, MAX_RETRY_WAIT)
        logger.warning("%s %s failed (%s); retrying in %.0fs", method, url, reason, wait)
        time.sleep(wait)


def bsky_login_session(pds_url: str, handle: str, password: str) -> Dict:
    """login to blueksy

    Args:
        pds_url (str): bsky platform (default for now)
        handle (str): username
        password (str): app password

    Returns:
        Dict: json blob with login
    """
    resp = request_with_retry(
        "POST",
        pds_url + "/xrpc/com.atproto.server.createSession",
        json={"identifier": handle, "password": password},
    )
    resp.raise_for_status()
    return resp.json()


def parse_urls(text: str) -> List[Dict]:
    """parse URLs in string blob

    Args:
        text (str): string

    Returns:
        List[Dict]: span of url
    """
    spans = []
    # partial/naive URL regex based on: https://stackoverflow.com/a/3809435
    # tweaked to disallow some training punctuation
    url_regex = rb"[$|\W](https?:\/\/(www\.)?[-a-zA-Z0-9@:%._\+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b([-a-zA-Z0-9()@:%_\+.~#?&//=]*[-a-zA-Z0-9@%_\+~#//=])?)"
    text_bytes = text.encode("UTF-8")
    for m in re.finditer(url_regex, text_bytes):
        spans.append(
            {
                "start": m.start(1),
                "end": m.end(1),
                "url": m.group(1).decode("UTF-8"),
            }
        )
    return spans


def parse_facets(text: str) -> List[Dict]:
    """
    parses post text and returns a list of app.bsky.richtext.facet objects for any URLs (https://example.com)
    """
    facets = []
    for u in parse_urls(text):
        facets.append(
            {
                "index": {
                    "byteStart": u["start"],
                    "byteEnd": u["end"],
                },
                "features": [
                    {
                        "$type": "app.bsky.richtext.facet#link",
                        # NOTE: URI ("I") not URL ("L")
                        "uri": u["url"],
                    }
                ],
            }
        )
    return facets


def _create_record(post: Dict[str, Any], session: Dict, pds_url: str) -> requests.Response:
    """Send one createRecord call.

    A new post is not idempotent, so only failures where the server certainly did
    not create it are retried: 429/503 and a failed connection attempt. A read
    timeout or a 500 is not retried, since the post may already exist.
    """
    return request_with_retry(
        "POST",
        pds_url + "/xrpc/com.atproto.repo.createRecord",
        headers={"Authorization": "Bearer " + session["accessJwt"]},
        json={
            "repo": session["did"],
            "collection": "app.bsky.feed.post",
            "record": post,
        },
        retry_status=frozenset({429, 503}),
        retry_errors=(requests.ConnectTimeout,),
    )


def create_post(
    text: str, session: Dict, pds_url: str = PDS_URL, card: Optional[Dict[str, Any]] = None
) -> str:
    """post on bluesky

    Args:
        text (str): post text; URLs in it become clickable links.
        session (Dict): result of ``bsky_login_session`` (one login serves many posts).
        pds_url (str, optional): bsky Defaults to "https://bsky.social".
        card (Dict, optional): link card from ``build_link_card``. If Bluesky rejects
            the post with a 400 while a card is attached, it is posted once more
            without the card, so a card problem can never stop the bot.

    Returns:
        str: AT URI of the new post.

    Raises:
        requests.HTTPError: if Bluesky rejects the post.
    """
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    # these are the required fields which every post must include
    post: Dict[str, Any] = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "createdAt": now,
        "langs": POST_LANGS,
    }

    # parse out mentions and URLs as "facets"
    facets = parse_facets(text) if text else []
    if facets:
        post["facets"] = facets
    if card:
        post["embed"] = card

    resp = _create_record(post, session, pds_url)
    if resp.status_code == 400 and card:
        logger.warning("Bluesky rejected the post with its link card (%s); retrying without it", resp.text[:300])
        resp = _create_record({k: v for k, v in post.items() if k != "embed"}, session, pds_url)
    if not resp.ok:
        logger.error("createRecord failed: %s %s", resp.status_code, resp.text[:500])
    resp.raise_for_status()
    uri = resp.json().get("uri", "")
    logger.info("posted %s", uri)
    return uri


def clean_abstract(description: str) -> str:
    """Abstract text from a feed description: header removed, entities decoded, one line."""
    text = description.split("Abstract:", 1)[-1]
    return " ".join(html.unescape(text).split())


def format_authors(author: str) -> str:
    """'A, B, C' -> 'A et al.'; two authors -> 'A and B'; empty -> ''."""
    names = [n.strip() for n in author.split(",") if n.strip()]
    if len(names) <= 1:
        return "".join(names)
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{names[0]} et al."


# end of a sentence: 3+ word characters, ".!?", then a capitalised word ("e.g. x", "vs. y" do not match)
_SENTENCE_END = re.compile(r"(?<=\w{3})[.!?](?=\s+[A-Z])")


# words a snippet should not end on
_DANGLING_WORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "nor", "of", "in", "on", "at", "to", "for", "with", "by",
    "from", "as", "is", "are", "was", "were", "be", "that", "which", "than", "via", "into",
})


def truncate_text(text: str, room: int) -> str:
    """Shorten ``text`` to at most ``room`` characters without cutting a word.

    Prefers ending on a full sentence; otherwise ends on a word and adds "…".
    Text that already fits is returned unchanged (no ellipsis).
    """
    if len(text) <= room:
        return text
    if room < 2:
        return ""
    cut = text[: room - 1]  # one char of room is kept for the ellipsis
    ends = [m.end() for m in _SENTENCE_END.finditer(cut)]
    if ends and ends[-1] >= room * 0.6 and cut[: ends[-1]].count("$") % 2 == 0:
        return cut[: ends[-1]]
    space = cut.rfind(" ")
    if space < 0:
        return ""  # one giant token (e.g. a URL): cutting it would break it
    kept = cut[:space]
    while kept.count("$") % 2 == 1:  # cut inside a LaTeX formula: drop the open "$..." part
        kept = kept[: kept.rfind("$")]
    words = kept.rstrip(" ,;:-–(").split(" ")
    while len(words) > 1 and words[-1].lower() in _DANGLING_WORDS:  # avoid "... of the…"
        words.pop()
    return " ".join(words).rstrip(" ,;:-–(") + "…"


def build_post_text(title: str, link: str, description: str, author: str = "") -> str:
    """Format a post as ``title / link / authors / abstract snippet``, at most 300 characters.

    The link is never cut (a cut link would lose its clickable facet) and the
    abstract never ends mid-word, so a URL inside it is never split either.
    Shortening order when space is tight: abstract first, then authors, then title.
    """
    authors = format_authors(author)
    tail = POST_TAIL

    def head(t: str, a: str) -> str:
        return "\n".join([t, link] + ([a] if a else [])) + "\n"

    h = head(title, authors)
    if len(h) + len(tail) > MAX_POST_LEN - MIN_ABSTRACT:
        h = head(title, "")  # no room for an abstract: drop the authors line first
    if len(h) + len(tail) > MAX_POST_LEN:
        room = MAX_POST_LEN - len(link) - 2 - len(tail)
        h = head(title[: max(room - 1, 0)].rstrip() + "…", "")
    room = MAX_POST_LEN - len(h) - len(tail)
    snippet = truncate_text(clean_abstract(description), room) if room >= MIN_ABSTRACT else ""
    return (h + snippet).rstrip() + tail


def build_link_card(link: str, title: str, description: str) -> Dict[str, Any]:
    """app.bsky.embed.external record: a clickable preview of the arXiv page.

    The lexicon requires only ``uri``, ``title`` and ``description``. There is no
    thumbnail on purpose: arXiv's only ``og:image`` is its generic logo, and
    fetching and uploading it for every post would add requests for no information.
    """
    return {
        "$type": "app.bsky.embed.external",
        "external": {
            "uri": link,
            "title": title,
            "description": truncate_text(clean_abstract(description), CARD_DESCRIPTION_LEN),
        },
    }


def get_arxiv_feed(subject: str = FEED_SUBJECT) -> Dict[str, Dict]:
    """get skeetable list of paper title, link, and (fragment of) abstract

    Args:
        subject (str): valid arxiv subject, e.g. "cs.si+physics.soc-ph"

    Returns:
        dict link -> {title, link, description}

    Raises:
        requests.RequestException: feed could not be downloaded.
        RuntimeError: response was not a parseable feed (an empty but valid feed,
            e.g. on weekends, is fine and returns {}).
    """
    resp = request_with_retry("GET", f"https://rss.arxiv.org/rss/{subject}")
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    if feed.bozo and not feed.entries:
        raise RuntimeError(f"Could not parse arXiv feed for {subject}: {feed.bozo_exception}")
    return {
        entry.link.strip(): {
            "title": entry.title.strip(),
            "link": entry.link.strip(),
            "description": entry.description.replace("<p>", "")
            .replace("</p>", "")
            .strip(),
            # not archived (see ARCHIVE_FIELDS), only used to decide whether/how to post
            "author": entry.get("author", ""),
            "announce_type": entry.get("arxiv_announce_type", ""),
        }
        for entry in feed.entries
    }


def load_archive(filename: str = ARCHIVE_FILE) -> Dict[str, Dict]:
    """Load the archive of already-posted papers ({} if the file does not exist)."""
    try:
        with open(filename, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_archive(archive: Dict[str, Dict], filename: str = ARCHIVE_FILE) -> None:
    """Write the archive atomically: a crash mid-write cannot corrupt the file."""
    directory = os.path.dirname(os.path.abspath(filename))
    fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(archive, f, indent=None)
        os.replace(tmp_path, filename)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


# %%
def main() -> int:
    """Post every feed entry that is not in the archive yet.

    An entry is archived only after its post succeeded, and the archive is saved
    after each post. A failed post is logged and left un-archived so the next run
    retries it; the remaining entries are still attempted.

    Returns:
        Process exit code: 0 if everything went out, 1 if any post failed.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    handle, password = get_credentials()
    archive = load_archive()
    feed = get_arxiv_feed()
    unseen = {k: v for k, v in feed.items() if k not in archive}
    new = {k: v for k, v in unseen.items() if v.get("announce_type") not in SKIP_ANNOUNCE_TYPES}
    logger.info(
        "%d entries in feed, %d not posted yet, %d skipped as revisions of older papers",
        len(feed), len(new), len(unseen) - len(new),
    )
    if not new:
        return 0

    session = bsky_login_session(PDS_URL, handle, password)
    logged_in_at = time.monotonic()
    failures = 0
    for i, (key, entry) in enumerate(new.items()):
        if time.monotonic() - logged_in_at > SESSION_MAX_AGE:
            session = bsky_login_session(PDS_URL, handle, password)
            logged_in_at = time.monotonic()
        text = build_post_text(entry["title"], entry["link"], entry["description"], entry.get("author", ""))
        card = build_link_card(entry["link"], entry["title"], entry["description"]) if USE_LINK_CARD else None
        try:
            create_post(text, session, card=card)
        except requests.RequestException:
            logger.exception("failed to post %s", key)
            failures += 1
            continue
        archive[key] = {f: entry[f] for f in ARCHIVE_FIELDS if f in entry}
        save_archive(archive)
        if i < len(new) - 1:
            time.sleep(random.randint(*SLEEP_RANGE))
    if failures:
        logger.error("%d of %d posts failed; they will be retried on the next run", failures, len(new))
    return 1 if failures else 0


# %%
if __name__ == "__main__":
    sys.exit(main())
