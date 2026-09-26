# jev-docs

Community-maintained history of Jev / TypeSafe System One APIs, SDKs, agent
guidance, patterns, and engineering best practices.

This is an unofficial reference project. It is not maintained by TypeSafe and
does not speak for TypeSafe.

## What is tracked

The synchronizer observes official TypeSafe sources only:

- the live documentation index at [`docs.typesafe.ai/llms.txt`](https://docs.typesafe.ai/llms.txt),
  its discovered Markdown pages, and the public sitemap;
- the official `typesafe-ai/skills` agent skill;
- the official Python and JavaScript SDK repositories, their public package
  metadata, Git tags/releases, and documented changelogs;

The TypeSafe-owned [`system-one-adapter-python`](https://github.com/typesafe-ai/system-one-adapter-python)
is tracked separately as an `official_evaluation_tool`: an LLM-backed,
System One-compatible comparison tool. Its version, immutable commit, documented
purpose and provider families are metadata, not Jev model capabilities, SDK
parity, API guarantees, recommendations, or benchmark rankings.
`typesafe-ai.github.io` remains excluded because it is a static landing page
rather than the authoritative documentation tree. Unrelated infrastructure,
demos and model-serving projects remain outside scope.

The synchronizer does not copy the hosted documentation wholesale. It records
URLs, discovery metadata, content hashes, timestamps, and small excerpts needed
to audit the generated state. This is deliberate: the SDK and skill repositories
are MIT-licensed, but no license for the hosted documentation was found. The
MIT license in this repository applies only to repository-authored code and
documentation. It does not relicense TypeSafe material.

## How to read the repository

- [`BEST_PRACTICES.md`](BEST_PRACTICES.md) is the concise current guide for
  humans and coding agents.
- [`MODEL_LIMITATIONS.md`](MODEL_LIMITATIONS.md) describes upstream-documented
  weaknesses of specific model versions, separately from the stable programming
  model and model identity/alias state. Missing guidance becomes `unknown`, not fixed.
- [`state/cookbooks.json`](state/cookbooks.json) lists official recipes by category
  and difficulty, with concise upstream descriptions. `indexed` and `discovered`
  record the cookbook index and `llms.txt` independently; neither overwrites the
  other. Descriptions can contain upstream example results; they are not validated
  benchmarks or general performance guarantees. For architecture-level patterns,
  use the [official pattern guide](https://docs.typesafe.ai/patterns); its pages
  are already discoverable in source coverage.
- [`state/`](state/) is deterministic structured current state. Every meaningful
  derived item carries source URLs and hashes.
- [`sources/`](sources/) contains source manifests, not a bulk web mirror.
- [`events/`](events/) contains append-safe semantic events. The initial
  [`events/baseline.json`](events/baseline.json) establishes a baseline and
  intentionally does not claim that every current item was added on bootstrap.
- [`changes/`](changes/) contains human-readable daily reports when semantic
  events occur.
- `state/sdk-python.json` and `state/sdk-javascript.json` keep the SDK release
  streams independent. A mutable `main` commit is never treated as a package
  release. Python capabilities are attributed only after matching a published
  package version, tag commit, release configuration and immutable changelog.
  JavaScript capability parity is not inferred; an empty capability list means
  this extractor has no independently established entries.

## Provenance and history

The repository distinguishes observed facts from derived interpretation:

1. **Observed evidence** is a URL, repository path, package response, immutable
   tag/commit where available, observation timestamp, and SHA-256 hash.
2. **Current state** is generated from the observations using deterministic,
   reviewable rules.
3. **Semantic summaries** such as practices are conservative interpretations of
   explicit canonical wording. They retain the matching source and excerpt.

Web documentation observation history starts at the first successful bootstrap;
the hosted site does not expose a trustworthy immutable page history through the
tracked surfaces. SDK and skill history can include earlier dates when Git tags,
releases, or commit history provide them. Unknown history stays unknown.

Practice statuses are `recommended`, `discouraged`, `anti_pattern`,
`deprecated`, or `unknown`. A practice disappearing from one page is not marked
deprecated automatically: removal requires explicit evidence, so a failed or
partial source fetch cannot erase a recommendation.

## Automation

GitHub Actions runs pull-request validation and a strict synchronization every
12 hours, with manual `workflow_dispatch`. Scheduled runs use concurrency
protection, require no TypeSafe API key, and commit only actual generated
changes. Per-attempt `newly_discovered` and `disappeared` IDs are returned by sync;
durable coverage contains the current complete inventory. This lets a successful
addition land without requiring a cleanup commit on the next unchanged run.
Failed scheduled/manual runs upload a small `sync-failed-attempt` artifact with
the baseline SHA, failure phase and available candidate counts/versions. It is
attempt metadata, never canonical history. A failed discovery or source fetch
leaves the last-known-good generated state untouched and fails loudly. An SDK package/tag discrepancy is allowed a
three-day observation grace period, then causes strict synchronization to fail
until the upstream evidence converges.

## Local usage

Python 3.10+ is supported. The tested uv version is pinned in `pyproject.toml`
and used by both local commands and setup-uv. With that `uv` version installed:

```bash
just setup
just lint
just test
just typecheck
just check
just sync
just check-strict
```

`just sync` performs the public-source observation and generation. `just
check-strict` validates generated JSON, provenance, and fixture-backed
determinism checks. Run `actionlint` separately to validate workflow syntax; the
live scheduled workflow additionally performs a strict sync. All commits in
this repository must use `git commit -s`.

## Scope intentionally deferred

Community posts, third-party integrations, `awesome-jev`, private APIs,
benchmarks, full AST compatibility analysis, and a full hosted-doc mirror are
outside scope. A provenance-separated `field-notes/` layer remains deferred;
community interest alone does not justify ingesting claims into canonical state.
