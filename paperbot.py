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
from typing import Any, Dict, List, Tuple

import feedparser
import requests
from bs4 import BeautifulSoup

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
REQUEST_TIMEOUT = 30  # seconds; without it a stalled connection hangs the job
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


def bsky_login_session(pds_url: str, handle: str, password: str) -> Dict:
    """login to blueksy

    Args:
        pds_url (str): bsky platform (default for now)
        handle (str): username
        password (str): app password

    Returns:
        Dict: json blob with login
    """
    resp = requests.post(
        pds_url + "/xrpc/com.atproto.server.createSession",
        json={"identifier": handle, "password": password},
        timeout=REQUEST_TIMEOUT,
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


def fetch_embed_url_card(access_token: str, url: str) -> Dict:
    # TODO make this work... :(  (currently unused; see docs/IMPROVEMENT_PLAN.md)
    # the required fields for every embed card
    card: Dict[str, Any] = {
        "uri": url,
        "title": "",
        "description": "",
    }

    # fetch the HTML
    resp = requests.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    # parse out the "og:title" and "og:description" HTML meta tags
    title_tag = soup.find("meta", property="og:title")
    if title_tag:
        card["title"] = title_tag["content"]

    description_tag = soup.find("meta", property="og:description")
    if description_tag:
        card["description"] = description_tag["content"]

    # find the first image tag that has alt="arxiv logo"
    image_tag = soup.find("img", alt="arxiv logo")
    # get the src attribute of the image tag
    if image_tag:
        img_url = str(image_tag["src"])
        # naively turn a "relative" URL (just a path) into a full URL, if needed
        if "://" not in img_url:
            img_url = url + img_url
        resp = requests.get(img_url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()

        blob_resp = requests.post(
            "https://bsky.social/xrpc/com.atproto.repo.uploadBlob",
            headers={
                "Content-Type": 'image/png',
                "Authorization": "Bearer " + access_token,
            },
            data=resp.content,
            timeout=REQUEST_TIMEOUT,
        )
        blob_resp.raise_for_status()
        card["thumb"] = blob_resp.json()["blob"]

    return {
        "$type": "app.bsky.embed.external",
        "external": card,
    }


def create_post(text: str, session: Dict, pds_url: str = PDS_URL) -> str:
    """post on bluesky

    Args:
        text (str): post text; URLs in it become clickable links.
        session (Dict): result of ``bsky_login_session`` (one login serves many posts).
        pds_url (str, optional): bsky Defaults to "https://bsky.social".

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

    resp = requests.post(
        pds_url + "/xrpc/com.atproto.repo.createRecord",
        headers={"Authorization": "Bearer " + session["accessJwt"]},
        json={
            "repo": session["did"],
            "collection": "app.bsky.feed.post",
            "record": post,
        },
        timeout=REQUEST_TIMEOUT,
    )
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
    resp = requests.get(f"https://rss.arxiv.org/rss/{subject}", timeout=REQUEST_TIMEOUT)
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
        try:
            create_post(text, session)
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
