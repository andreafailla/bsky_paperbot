# bsky_paperbot: improvement plan

Written 2026-11-21 against `andreafailla/bsky_paperbot` (fork of `apoorvalal/bsky_paperbot`).
Numbers below were measured from the repo, its GitHub Actions history and the live arXiv feed on that date.

## What was fixed in this branch

| # | Problem (evidence) | Fix |
|---|---|---|
| 1 | **Bot down since 2026-11-02**: 49 consecutive failed runs; every run died at `setup-python` with `version '3.9' ... was not found for Ubuntu 26.04` (`ubuntu-latest` moved to 26.04, Python 3.9 is EOL). | Python 3.12; `checkout@v6`, `setup-python@v6` (the old majors triggered Node 20 deprecation warnings). |
| 2 | Titles cut at the first period (`title.split(".")[0]`, a leftover from an older feed format). **0 of 8,945 archived titles contain a "."**, so "2.5D", "vs.", "U.S." titles were always truncated. | Removed. Live feed titles carry no `(arXiv:...)` suffix, so nothing else was relying on it. |
| 3 | Archive written **before** posting. A failed post at item *k* marked items *k..n* as done without posting them (reproduced with a mock). In CI the commit step was skipped on failure, so the next run re-posted items that had already gone out (duplicates). | Entry archived only after its post succeeds; `combined.json` saved atomically after each post; failed items are retried next run; commit step runs `if: always()` so progress survives a partial failure; exit code 1 if any post failed. |
| 4 | `[:293]` sliced the whole text, so a long title cut the link in half: no clickable facet (reproduced). | `build_post_text` shortens only the abstract (and the title as a last resort). |
| 5 | `os.environ["BSKYBOT"]` as default argument: `import paperbot` raised `KeyError` without secrets, which made testing impossible. | Credentials read in `get_credentials()` with a clear error message. |
| 6 | No HTTP timeouts anywhere (a stalled connection hangs the job until GitHub's 6 h limit). A broken/blank feed response silently looked like "no new papers". | `timeout=30` on every request; feed fetched with `requests`, non-200 raises; unparsable body raises. |
| 7 | One `createSession` login per post (rate-limit exposure); sleeping after the last post. | One session per run (refreshed after 50 min); no trailing sleep. |
| 8 | Workflow: invalid `repository:`/`ref:` inputs on checkout (empty on `schedule`), no `concurrency` (overlapping runs double-post and race on push), no `permissions`, no timeout, `add: "."`. | Removed the inputs; `concurrency`, `permissions: contents: write`, `timeout-minutes: 120`, commit only `combined.json`. |
| 9 | README badge pointed at the upstream repo; no tests. | Badge fixed; offline pytest suite in `tests/` (33 tests). |

Verification done: 33/33 tests pass; mypy clean; dry run against the live feed and a copy of the real archive (posting mocked) produced 9 posts, all ≤300 chars with clickable links, repo `combined.json` untouched; workflow YAML parses.
**Not verified:** a real GitHub Actions run of the new workflow (needs a push; see "Rollout" below).

## Post quality (second pass, done)

Checked on all 8,945 archived descriptions: 0 posts over 300 chars, 0 non-clickable links, 0 cuts inside a word, 0 broken `$...$` formulas.

| Before | After |
|---|---|
| Abstract cut at a fixed character, often mid-word (`...estab...`), and a URL in the abstract could be cut into a wrong link | Cut at a full sentence if one fits in at least 60% of the room, otherwise at a word; `…` only when text was actually cut (a short abstract is no longer followed by `...`); never ends on "and", "of", "the"...; never splits a URL |
| Formulas cut open (`$E = mc^2 +…`): 3 of 16 abstracts in today's feed contain LaTeX | Open `$...` part dropped from the snippet |
| HTML entities (`&amp;`) and stray newlines left in the text | Decoded and collapsed to one line |
| Revised older papers (arXiv `replace`, `replace-cross`) posted as if new: 11% of the archive | Skipped, not archived. Constant `SKIP_ANNOUNCE_TYPES` in `paperbot.py`; set it to `frozenset()` to post them again |
| No authors | Line with `A`, `A and B` or `A et al.`; dropped first if the title leaves no room |
| No language tag | `langs: ["en"]` |
| Archive entries only needed for de-duplication | Same three fields as before (`title`, `link`, `description`), so `combined.json` stays compatible |

Link cards were added in the third pass below.

## Third pass (done)

| Change | Detail |
|---|---|
| **Link card** | Each post carries an `app.bsky.embed.external` card (`uri`, `title`, abstract up to 500 chars). Switch off with `USE_LINK_CARD = False`. If Bluesky answers 400 to a post with a card, the same post is sent once without it, so a card problem can never stop the bot. |
| **No thumbnail** | arXiv abs pages do have an `og:image`, but it is the generic arXiv logo (an earlier version of this document claimed there was none). Fetching and uploading it per post adds two requests for no information, so there is no `thumb`. This also removed the dead, broken `fetch_embed_url_card` and the `beautifulsoup4` dependency. |
| **Retry with backoff** | Feed download, login and posting retry 429/5xx and connection errors after 5, 20, 60 s; a `Retry-After` / `ratelimit-reset` wait is honoured (capped at 300 s). **Posting is retried only on 429/503 and failed connection attempts**: after a read timeout or a 500 the post may already exist, and repeating it would be a duplicate. |
| **Failure alerts** | A failed run opens (or comments on) a `bot-failure` issue; the next good run closes it. Needs `issues: write` (set in the workflow). |
| **Tests in CI** | `test.yml` runs pytest on every push to `master` and on pull requests. `tests/conftest.py` makes any un-mocked HTTP request fail the test (see below). |
| **Pinning** | `runs-on: ubuntu-24.04` instead of `ubuntu-latest`; `.github/dependabot.yml` (monthly, actions and pip); `requirements.txt` lower bounds. |

What was checked: 49 tests pass, mypy clean. A dry run read the live arXiv feed and intercepted every POST to Bluesky at the HTTP layer: 9 records, all valid against the official `app.bsky.feed.post` and `app.bsky.embed.external` lexicons (required fields, no unknown fields, text ≤ 300 graphemes, `langs` ≤ 3).
**Not verified:** how the card renders in the Bluesky app (needs a real post); the failure-issue steps and `test.yml` on GitHub (YAML and shell syntax were checked, they have not run).

A mistake worth recording: while moving the HTTP calls to a retry helper, one test still mocked the old function and sent a real request to `bsky.social` with a dummy token (answer: 400 InvalidToken, nothing was posted). `tests/conftest.py` now blocks real HTTP in every test.

## Planned improvements

### P0: decide or do soon

1. ~~**Stop posting revised papers as new.**~~ Done in the post-quality pass (see above).
2. ~~**Failure alerts.**~~ Done (third pass).
3. ~~**Pin what drifted.**~~ Done (third pass): `ubuntu-24.04`, Dependabot, lower bounds. Python stays at 3.12.
4. **Rollout of this branch** (see below).

### P1: next

5. **Archive bloat.** `combined.json` is 13.9 MB; 12.2 MB (88%) is abstract text that is never read again, because only the keys are used for de-duplication. Each bot commit rewrites the whole single-line file: a fresh clone downloads a **105 MiB** pack for 836 commits (GitHub reports 31 MB server-side; an earlier figure of 37 MB in this document was measured on a half-finished clone and was wrong). CI is not affected (checkout is shallow), clones and backups are. Plan: new append-only `posted.txt` (one arXiv id per line, ~0.3 MB), one-off migration script from `combined.json`, loader that reads either, then stop writing abstracts. Old history stays large unless rewritten, which is not recommended.
6. ~~**Link cards.**~~ Done (third pass).
7. ~~**Retry with backoff.**~~ Done (third pass).
8. ~~**Tests in CI.**~~ Done (third pass): `.github/workflows/test.yml`.
9. **Schedule: keep 5 runs/day.** I wanted to cut it to 2, but the feed's `lastBuildDate` was 23:25 UTC while entries are dated 00:00 EST, so builds are not tied to one moment. Re-check over a week of logged feed timestamps before touching the cron. Caveat if state ever leaves the repo: GitHub disables schedules after 60 days without repo activity.
10. ~~`langs: ["en"]` on posts~~ Done.

### P2: when convenient

11. **More feeds**: `FEEDS` env var (comma-separated), de-duplicated across feeds (cross-lists appear twice).
12. **Supply chain**: pin third-party actions to commit SHAs, or replace `EndBug/add-and-commit@v9` (v11 is current) with three `git` commands.
13. **Secrets hygiene**: `BSKYBOT`/`BSKYPWD` were last set 2024-05-10. Confirm `BSKYPWD` is an app password; rotate if unsure.
14. **Repo hygiene**: `.idea/` is tracked (add to `.gitignore`, `git rm -r --cached .idea`); add `ruff`/`mypy` config; fork has diverged from upstream (upstream's bot also commits to its own `combined.json`, so a merge would conflict on every data commit; do not merge upstream wholesale. Upstream's added Typst step is unused by the code).
15. **Step summary**: write posted/failed counts to `$GITHUB_STEP_SUMMARY`.
16. **Split `paperbot.py`** (455 lines, over the 400-line guideline): `text.py` (clean/truncate/build post), `bsky.py` (retry, session, createRecord, card), `feed.py` (arXiv feed, archive), and a thin `paperbot.py` with `main()`. Do it as a pure move with the tests as safety net; the workflow still calls `python3 paperbot.py`.

## Rollout of this branch

Expect the first run after the fix to post the papers currently in the feed that your stale archive does not know (3 `new`, 6 `cross`; the 1 `replace` is now skipped, so 9 posts), spaced 1-5 min apart, about 30 min total.

1. Review the diff, commit, push the branch, open a PR to `master` (`test.yml` runs the tests on the pull request).
2. Merge, then trigger once: `gh workflow run post.yml -R andreafailla/bsky_paperbot`. **This posts for real** with the repo secrets. Do not run it from the branch unless you want those posts to come from the branch.
3. Watch it: `gh run watch -R andreafailla/bsky_paperbot`. Always pass `-R`: in a clone with an `upstream` remote, a bare `gh run list` resolves to the upstream repository and shows its history, not yours.
