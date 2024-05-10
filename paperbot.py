import time
import re
import os
import sys
import json
from typing import List, Dict
from datetime import datetime, timezone
import random

import requests
import feedparser
from bs4 import BeautifulSoup


# %%
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
    )
    resp.raise_for_status()
    resp = resp.json()
    return resp


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
    # TODO make this work... :(
    # the required fields for every embed card
    card = {
        "uri": url,
        "title": "",
        "description": "",
    }

    # fetch the HTML
    resp = requests.get(url)
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
        img_url = image_tag["src"]
        # naively turn a "relative" URL (just a path) into a full URL, if needed
        if "://" not in img_url:
            img_url = url + img_url
        resp = requests.get(img_url)
        resp.raise_for_status()

        blob_resp = requests.post(
            "https://bsky.social/xrpc/com.atproto.repo.uploadBlob",
            headers={
                "Content-Type": 'image/png',
                "Authorization": "Bearer " + access_token,
            },
            data=resp.content,
        )
        blob_resp.raise_for_status()
        card["thumb"] = blob_resp.json()["blob"]

    return {
        "$type": "app.bsky.embed.external",
        "external": card,
    }


def create_post(
        text: str,
        pds_url: str = "https://bsky.social",
        handle: str = os.environ["BSKYBOT"],
        password: str = os.environ["BSKYPWD"],
):
    """post on bluesky

    Args:
        text (str): text
        pds_url (str, optional): bsky Defaults to "https://bsky.social".
        handle (_type_, optional):  Defaults to os.environ["BSKYBOT"]. Set this environmental variable in your dotfile (bashrc/zshrc).
        password (_type_, optional): _description_. Defaults to os.environ["BSKYPWD"].
    """
    session = bsky_login_session(pds_url, handle, password)
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    # these are the required fields which every post must include
    post = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "createdAt": now,
    }

    # parse out mentions and URLs as "facets"
    if len(text) > 0:
        facets = parse_facets(post["text"])
        if facets:
            post["facets"] = facets

            # add link embed according to the URL
            #
            #post["embeds"] = fetch_embed_url_card(session["accessJwt"], facets[0]["features"][0]["uri"])

    resp = requests.post(
        pds_url + "/xrpc/com.atproto.repo.createRecord",
        headers={"Authorization": "Bearer " + session["accessJwt"]},
        json={
            "repo": session["did"],
            "collection": "app.bsky.feed.post",
            "record": post,
        },
    )
    print("createRecord response:", file=sys.stderr)
    print(json.dumps(resp.json(), indent=2))
    resp.raise_for_status()


def get_arxiv_feed(subject: str = "cs.si+physics.soc-ph"):
    """get skeetable list of paper title, link, and (fragment of) abstract

    Args:
        subject (str): valid arxiv subject, defaults to combined econ.EM and stat.ME

    Returns:
        list of skeets
    """
    feed_url = f"https://rss.arxiv.org/rss/{subject}"
    feed = feedparser.parse(feed_url)
    # dict of all entries
    res = {
        entry.link.strip(): {
            "title": entry.title.split(".")[0].strip(),
            "link": entry.link.strip(),
            "description": entry.description.replace("<p>", "")
            .replace("</p>", "")
            .strip(),
        }
        for entry in feed.entries
    }
    return res


def get_and_write_feed_json(feedname="cs.si+physics.soc-ph", filename="combined.json"):
    feed = get_arxiv_feed(feedname)
    try:
        with open(filename, "r") as f:
            archive = json.load(f)
    except FileNotFoundError:  # if file doesn't exist
        archive = {}
    new_archive = archive.copy()
    # append new items
    for k, v in feed.items():
        if k not in archive:
            new_archive[k] = v
    # write out only if new items exist
    if len(new_archive) > len(archive):
        with open(filename, "w") as f:
            json.dump(new_archive, f, indent=None)
        print(f"{filename} updated")
    return feed, archive


# %%
def main():
    pull, archive = get_and_write_feed_json()
    ######################################################################
    # stats
    ######################################################################
    # read existing data from "stat_me_draws.json" file
    new_posts = 0
    # Append new data to existing data
    for k, v in pull.items():
        if k not in archive:  # if not already posted
            post_str = (
                    f"{v['title']}\n{v['link']}\n{''.join(v['description']).split('Abstract:')[-1].strip()}"[:293] + "...📈🤖"
            )
            create_post(post_str)
            time.sleep(random.randint(60, 300))
            archive[k] = v
            new_posts += 1
    if new_posts == 0 & (len(archive) > 2):
        post_str = "No new papers today! 📈🤖"
        create_post(post_str)


# %%
if __name__ == "__main__":
    main()

# %%
