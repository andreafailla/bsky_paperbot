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
| 9 | README badge pointed at the upstream repo; no tests. | Badge fixed; 15 offline pytest tests in `tests/`. |

Verification done: 15/15 tests pass; dry run against the live feed and a copy of the real archive (posting mocked) produced 10 posts, all ≤300 chars with clickable links, repo `combined.json` untouched; workflow YAML parses.
**Not verified:** a real GitHub Actions run of the new workflow (needs a push; see "Rollout" below).

## Planned improvements

### P0: decide or do soon

1. **Stop posting revised papers as new (needs your decision).** The feed mixes `new`, `cross`, `replace`, `replace-cross`. The archive holds 976 `replace*` entries (11%), e.g. `2211.06352v3` announced as a "new" paper. 1 of the 10 posts pending today is a `replace`. Fix: skip `replace`/`replace-cross` in `main()` (still archive them so they are not reconsidered). Make the set of allowed types a constant. I left behaviour unchanged because it changes what your bot publishes.
2. **Failure alerts.** The bot was red for 19 days without anyone noticing. Options: a final `if: failure()` step that opens/updates a GitHub issue (`gh issue create`), or a staleness check ("no post in 4 days" while the feed has entries). Pick one; the issue approach needs no external service.
3. **Pin what drifted.** The outage was caused by `ubuntu-latest` + an old Python. Pin `runs-on: ubuntu-24.04`, keep `python-version: '3.12'`, add `.github/dependabot.yml` for `github-actions` and `pip`, and give `requirements.txt` lower bounds (or a lock file).
4. **Rollout of this branch** (see below).

### P1: next

5. **Archive bloat.** `combined.json` is 13.9 MB; 12.2 MB (88%) is abstract text that is never read again, because only the keys are used for de-duplication. Each bot commit rewrites the whole single-line file, so the clone is 37 MB after 834 commits and grows daily. Plan: new append-only `posted.txt` (one arXiv id per line, ~0.3 MB), one-off migration script from `combined.json`, loader that reads either, then stop writing abstracts. Optional: keep metadata in a separate JSONL if you want the dataset.
6. **Link cards.** `fetch_embed_url_card` is dead and broken: wrong record key (`post["embeds"]`, must be `embed`), relative image URL built as `url + img_url`, hard-coded `image/png`, hard-coded `bsky.social`, and arXiv abs pages have no `og:image`. Either delete it, or rewrite it to a card with title/description only (no thumbnail) and call it from `create_post`.
7. **Retry with backoff.** On 429/5xx wait (`ratelimit-reset` header) and retry 3 times instead of waiting for the next run.
8. **Tests in CI.** Add a `pull_request`/`push` workflow running `pytest` (0.2 s). Keep it separate from the posting workflow so a test failure cannot silence the bot.
9. **Schedule.** 5 cron runs/day, but arXiv RSS publishes once a day. To verify first (check feed `pubDate` over a few days), then cut to 2 runs (primary + one retry). Caveat: GitHub disables schedules after 60 days with no repo activity; today the bot's own commits keep it alive, so if state moves out of the repo (item 5), add a keep-alive.
10. **`langs: ["en"]` on posts** for feed/discovery tooling; trivial.

### P2: when convenient

11. **More feeds**: `FEEDS` env var (comma-separated), de-duplicated across feeds (cross-lists appear twice).
12. **Supply chain**: pin third-party actions to commit SHAs, or replace `EndBug/add-and-commit@v9` (v11 is current) with three `git` commands.
13. **Secrets hygiene**: `BSKYBOT`/`BSKYPWD` were last set 2024-05-10. Confirm `BSKYPWD` is an app password; rotate if unsure.
14. **Repo hygiene**: `.idea/` is tracked (add to `.gitignore`, `git rm -r --cached .idea`); add `ruff`/`mypy` config; fork has diverged from upstream (upstream's bot also commits to its own `combined.json`, so a merge would conflict on every data commit; do not merge upstream wholesale. Upstream's added Typst step is unused by the code).
15. **Step summary**: write posted/failed counts to `$GITHUB_STEP_SUMMARY`.

## Rollout of this branch

Expect the first run after the fix to post the 10 papers currently in the feed that your stale archive does not know (3 `new`, 6 `cross`, 1 `replace`), spaced 1-5 min apart, about 30 min total.

1. Review the diff, commit, push the branch, open a PR to `master` (tests do not run in CI yet, see item 8).
2. Merge, then trigger once: `gh workflow run post.yml -R andreafailla/bsky_paperbot`. **This posts for real** with the repo secrets. Do not run it from the branch unless you want those posts to come from the branch.
3. Watch it: `gh run watch -R andreafailla/bsky_paperbot`. Always pass `-R`: in a clone with an `upstream` remote, a bare `gh run list` resolves to the upstream repository and shows its history, not yours.
