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

New limitations output uses labeled, independently authored interpretations,
matched to the exact reviewed model and section hash, rather than copying
mitigation paragraphs. Unreviewed or changed sections have unknown summaries
and link to official evidence. This is not a licensing determination: hosted
documentation permissions remain uncertain. Historical state and events are
not rewritten; their older excerpts remain part of the recorded history.

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
  package name and version, tag commit, release configuration and immutable changelog.
  Tag commits must be full 40-character hexadecimal GitHub SHAs; malformed
  identities are explicit release-metadata discrepancies, never source pins
  for current or historical releases. Unresolved release runtime stays unknown.
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

API claims use only explicit canonical reference declarations, not fenced
examples. Unsupported attribute syntax or an unknown question-type declaration
leaves the affected claim unknown with a warning rather than verifying a partial
list. Conflicting requiredness does not erase independently known request field
names. Response key correlation requires a recognized affirmative source sentence;
evidence excerpts remain verbatim. A required model's example alias is not a
default, and correcting local assumptions is not an upstream history event.
If canonical source bytes also changed during that migration, the API baseline
records the first verified snapshot without inferring historical API behavior;
documentation source events independently record the changed bytes.

Practice statuses are `recommended`, `discouraged`, `anti_pattern`,
`deprecated`, or `unknown`. A practice disappearing from one page is not marked
deprecated automatically: removal requires explicit evidence, so a failed or
partial source fetch cannot erase a recommendation.

## Automation

GitHub Actions runs pull-request validation and a strict synchronization every
12 hours, with manual `workflow_dispatch` restricted to `main`. Concurrency
protection is unchanged. The ingestion/validation job has read-only repository
permission, uses public sources without TypeSafe API keys, and never executes
upstream code. All actions are pinned to immutable commits; the focused
Renovate configuration maintains GitHub Actions digests. Checkouts never persist
credentials.

Only after sync completes promotion and the full checks and strict tree validation
pass does ingestion upload a publication artifact: a generated-only Git patch
(at most 16 MiB) and a small manifest binding its SHA-256 digest to the baseline
commit, workflow run and run attempt. Artifact ID, baseline and eligibility cross
the job boundary only as explicit outputs. The separate write-permission job
checks out trusted scripts at that baseline, downloads that exact same-run
artifact, verifies provenance and the current `origin/main` SHA, and uses
`git apply --check` before applying. It rejects unexpected artifact entries,
symlinks, executable modes, traversal and paths outside the generated JSON,
dated reports and generated guides. After applying, it independently validates the
complete generated tree with the unchanged trusted Python code before the
token-bearing commit step. No downloaded scripts, dependency setup or ingested
source are executed with the publication token. A signed-off commit uses
a normal push, so a concurrent update to `main` is rejected rather than overwritten.
An unchanged run creates no commit.

Per-attempt `newly_discovered` and `disappeared` IDs are returned by sync;
durable coverage contains the current complete inventory. This lets a successful
addition land without requiring a cleanup commit on the next unchanged run.
Failed scheduled/manual runs upload a small `sync-failed-attempt` artifact with
the baseline SHA, failure phase and available candidate counts/versions. It is
attempt metadata, never canonical history. Failed discovery or source fetch fails
loudly without promoting that observation. A restart first recovers any earlier
validated promotion, retaining its original events before observing new sources.
An SDK package/tag discrepancy has a three-day observation grace period. Once expired,
strict sync remains visibly failed, but explicitly eligible, fully promoted
independent changes can still publish; a final health job also fails after the
publication job finishes. Other failures cannot authorize publication. Older
successful sync diagnostics without an explicit eligibility flag remain supported
only when their phase is `sync-complete` and result is `success`.

## Local usage

Python 3.10+ is supported. Python 3.11+ uses the standard-library `tomllib`
parser; Python 3.10 installs the conditional `tomli>=2,<3` backport for the
same project-scoped release verification. The tested uv version is pinned in
`pyproject.toml` and used by both local commands and setup-uv.
With that `uv` version installed:

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
check` validates generated JSON and provenance. `just check-strict` additionally
requires event IDs to be unique across all history, each `changes/` report to be
the rendering of its `events/` file, and `BEST_PRACTICES.md` to match its state.

If interrupted promotion leaves `.jev-docs-pending/`, validation fails explicitly.
Rerun `just sync` to recover the original batch; do not delete the pending journal.
The ignored `.jev-docs.lock` serializes local writers and stays in place after use.
Recurring transitions retain distinct predecessor-linked event IDs, while retries
and unchanged observations remain deduplicated. See [the design](docs/DESIGN.md)
for recovery boundaries.

Run `actionlint` separately to validate workflow syntax; the
live scheduled workflow additionally performs a strict sync. All commits in
this repository must use `git commit -s`.

## Scope intentionally deferred

Community posts, third-party integrations, `awesome-jev`, private APIs,
benchmarks, full AST compatibility analysis, and a full hosted-doc mirror are
outside scope. A provenance-separated `field-notes/` layer remains deferred;
community interest alone does not justify ingesting claims into canonical state.
