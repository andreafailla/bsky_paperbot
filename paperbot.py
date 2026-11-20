"""Bluesky bot: reads an arXiv RSS feed and posts papers it has not posted yet.

State lives in ``combined.json`` (link -> entry). An entry is added to it only
after its post succeeded, and the file is rewritten after every post, so a crash
or API error can neither lose a paper nor cause a double post.
"""

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
POST_SUFFIX = "...📈🤖"
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


def build_post_text(title: str, link: str, description: str) -> str:
    """Format ``title / link / abstract`` so it fits in one post.

    Only the abstract (and, as a last resort, the title) is shortened. The link
    is never cut: a cut link would lose its clickable facet.
    """
    abstract = description.split("Abstract:")[-1].strip()
    # budget left for the title once link, two newlines and suffix are reserved
    title_room = MAX_POST_LEN - len(link) - 2 - len(POST_SUFFIX)
    if len(title) > title_room:
        title = title[: max(title_room - 1, 0)].rstrip() + "…"
    head = f"{title}\n{link}\n"
    room = MAX_POST_LEN - len(head) - len(POST_SUFFIX)
    return head + abstract[: max(room, 0)] + POST_SUFFIX


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
    new = {k: v for k, v in feed.items() if k not in archive}
    logger.info("%d entries in feed, %d not posted yet", len(feed), len(new))
    if not new:
        return 0

    session = bsky_login_session(PDS_URL, handle, password)
    logged_in_at = time.monotonic()
    failures = 0
    for i, (key, entry) in enumerate(new.items()):
        if time.monotonic() - logged_in_at > SESSION_MAX_AGE:
            session = bsky_login_session(PDS_URL, handle, password)
            logged_in_at = time.monotonic()
        text = build_post_text(entry["title"], entry["link"], entry["description"])
        try:
            create_post(text, session)
        except requests.RequestException:
            logger.exception("failed to post %s", key)
            failures += 1
            continue
        archive[key] = entry
        save_archive(archive)
        if i < len(new) - 1:
            time.sleep(random.randint(*SLEEP_RANGE))
    if failures:
        logger.error("%d of %d posts failed; they will be retried on the next run", failures, len(new))
    return 1 if failures else 0


# %%
if __name__ == "__main__":
    sys.exit(main())
