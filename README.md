# bsky_paperbot

sourcecode for paper poster bot. Bot on bluesky that pings arxiv rss feeds and posts them on bluesky. Querier + archiver may be of independent interest.

## make your own bsky bot 

+ fork this repository
+ create a bluesky account 
+ get a bluesky password / username, and set them in `Settings > Secrets and Variables > BSKYBOT, BSKYPWD`
+ use different RSS feeds (`FEED_SUBJECT` in `paperbot.py`), or do something else 

`BSKYPWD` should be a Bluesky [app password](https://bsky.app/settings/app-passwords), not your account password.

## develop

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest          # offline: network and Bluesky are mocked, no credentials needed
```

Posted papers are tracked in `combined.json`. A paper is added only after its post succeeded, so failed posts are retried on the next run.

Knobs at the top of `paperbot.py`: `FEED_SUBJECT` (arXiv categories), `USE_LINK_CARD` (attach a link card to each post), `SKIP_ANNOUNCE_TYPES` (arXiv revisions are not posted), `SLEEP_RANGE` (seconds between posts). If a run fails, the workflow opens a `bot-failure` issue; the next good run closes it.

See [docs/IMPROVEMENT_PLAN.md](docs/IMPROVEMENT_PLAN.md) for known gaps and planned work.

[![GH Arxiv Posterbot](https://github.com/andreafailla/bsky_paperbot/actions/workflows/post.yml/badge.svg?branch=master)](https://github.com/andreafailla/bsky_paperbot/actions/workflows/post.yml)
