# jev-docs

Community-maintained history of Jev / TypeSafe System One APIs, SDKs, agent
guidance, patterns, and engineering best practices.

This is an unofficial reference project. It is not maintained by TypeSafe and
does not speak for TypeSafe.

## What is tracked

V1 observes official TypeSafe sources only:

- the live documentation index at [`docs.typesafe.ai/llms.txt`](https://docs.typesafe.ai/llms.txt),
  its discovered Markdown pages, and the public sitemap;
- the official `typesafe-ai/skills` agent skill;
- the official Python and JavaScript SDK repositories, their public package
  metadata, Git tags/releases, and documented changelogs;

The directly relevant `system-one-adapter-python` repository was investigated
but is excluded from canonical state: it is a drop-in LLM-backed alternative,
not an official TypeSafe product surface. The `typesafe-ai.github.io` repository
was also checked and excluded because it is a static landing page rather than
the authoritative documentation tree. Other organization repositories are
unrelated infrastructure, demos, or model-serving projects and are outside V1.

The synchronizer does not copy the hosted documentation wholesale. It records
URLs, discovery metadata, content hashes, timestamps, and small excerpts needed
to audit the generated state. This is deliberate: the SDK and skill repositories
are MIT-licensed, but no license for the hosted documentation was found. The
MIT license in this repository applies only to repository-authored code and
documentation. It does not relicense TypeSafe material.

## How to read the repository

- [`BEST_PRACTICES.md`](BEST_PRACTICES.md) is the concise current guide for
  humans and coding agents.
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
  release.

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
changes. A failed discovery or source fetch leaves the last-known-good generated
state untouched and fails loudly.

## Local usage

Python 3.10+ is supported. With `uv` installed:

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
check-strict` validates generated JSON, provenance, workflow shape, and the
fixture-backed determinism checks; the live scheduled workflow additionally
performs a strict sync. All commits in this repository must use `git commit -s`.

## Scope intentionally deferred

Community posts, third-party integrations, `awesome-jev`, private APIs,
benchmarks, full AST compatibility analysis, and a full hosted-doc mirror are
outside V1. A future `field-notes/` layer may add clearly separated community
evidence without mixing it into canonical state.
