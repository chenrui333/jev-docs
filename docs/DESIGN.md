# Synchronizer design

## Evidence boundary

The canonical web discovery input is `https://docs.typesafe.ai/llms.txt`.
The synchronizer follows same-host links from that index and records the public
sitemap's `lastmod` values when available. If the canonical index is unavailable, the run fails and preserves old state.
A working sitemap cannot establish removals from a failed canonical index. Mintlify's `.md` representation is used for Markdown
fetches, with the normal page form as a bounded fallback when a Markdown
response is unavailable.

GitHub and package registries are separate immutable-or-mutable provenance
streams. A released SDK is identified by package version, matching Git tag, and
tag commit when all are available. `main` is captured as current source context
but is never substituted for a release. Python and JavaScript state is never
merged into one version stream.

Hosted documentation is not bulk mirrored. There is no documentation license in
the discovered legal or repository surfaces that would justify doing so. V1
retains metadata, hashes, source URLs, and short matching excerpts used by
derived claims. Repository code is MIT-licensed; that does not relicense
TypeSafe documentation.

The organization inventory was checked during bootstrap. `skills`,
`typesafe-sdk-python`, and `typesafe-sdk-js` are included. The
`system-one-adapter-python` project is observed separately as an
`official_evaluation_tool`, using its stable release tag/commit, versioned README
and configuration. It is an LLM-backed alternative and never a Jev capability
or API/SDK guarantee. Provider families are explicit README evidence, not parity.
`typesafe-ai.github.io` is a static landing page and is excluded. LLaDA,
Overwatch, daggerverse, pulumi-clickhouse, vllm, and other organization
projects are unrelated to the Jev/System One documentation evidence boundary.

## Generated layers

- `sources/` contains one manifest per source family.
- `state/` contains stable JSON snapshots with explicit schema versions and
  provenance.
- `state/source-coverage.json` records discovered, fetched, retained,
  and intentionally excluded pages. Per-attempt newly discovered and disappeared
  IDs are returned by sync rather than persisted: clearing a transient delta must
  not require a follow-up commit. Successful
  discovery sets `removal_safe`; failed discovery never promotes a replacement
  coverage snapshot.
- `BEST_PRACTICES.md` is rendered only from `state/practices.json`.
- `events/` contains stable-ID semantic changes. `events/baseline.json` marks
  the initial snapshot without manufacturing additions.
- `changes/` renders the events for a day. Existing historical reports are not
  regenerated; only the current day's report can receive additional events.

The generated state uses stable JSON ordering and preserves an artifact's first
observation timestamp while its content hash is unchanged. This avoids clock-
driven churn while retaining meaningful observation timestamps. Events use a
SHA-256 identity over schema, entity, change type, and before/after hashes.

## Practices

V1 uses an explicit table of conservative extraction rules in
`src/jev_docs/sync.py`. Each rule has a stable ID, category, status, summary,
and one or more source patterns. A rule becomes `recommended` only when current
canonical text matches; absent evidence becomes `unknown`, not deprecated.
Matching excerpts are retained in the derived practice item so a reader can
audit the interpretation without downloading the full source.

## Failure and freshness semantics

Discovery and required source fetches happen before any repository output is
promoted. A failed run exits nonzero and leaves generated state untouched. This
is the last-known-good guarantee. GitHub Actions runs strict validation and a
12-hour schedule with concurrency protection; it can push only generated
changes using the standard token. Freshness reports whether the observed package
metadata, tags, skill commit, and docs discovery agree at the time of a
successful run. The first SDK release discrepancy is observed with a three-day
grace period; if the same discrepancy remains after that period, strict
synchronization fails before promotion. A transient fetch failure remains an
immediate failed run and does not update freshness.

## Deferred work

V1 does not reconstruct a mutable hosted-doc timeline, mirror full pages, run
AST compatibility analysis, ingest community material, or infer deprecation from
absence. Those features require stronger evidence and should remain separate
from the canonical layer.

## Structured official guidance

`state/model-limitations.json` uses IDs scoped by the page's explicit model
applicability and section anchor. Second- and third-level headings retain the
upstream distinctions, including numeric/counting/score subtopics. Summaries and
mitigations are bounded source excerpts, not global claims. Missing sections or
pages retain last-known evidence with `unknown` status; a successful observation
can change that status back to `documented`. No absence establishes a fix. A
later model receives independent IDs and cannot overwrite the older model.
`MODEL_LIMITATIONS.md` is a deterministic rendering of this state.

`state/cookbooks.json` reads the index's category, link, description and level
columns. Unknown table formats fail before promotion. It unions index entries
with discovered cookbook pages, keeping membership in each surface explicit;
category and level stay null for discovery-only entries. Removal means absent
from both successfully observed navigation surfaces, never deprecation. A missing
previously tracked index fails rather than erasing the inventory. A separate
patterns inventory is deferred: existing page metadata and the official pattern
index already expose it, without a second hierarchy to maintain.

The coding-agent practices apply to bounded typed decisions inside software,
not replacement of a generative agent's base model or every JSON task. Evidence
excerpts remove known presentation wrappers and prefer paragraph/list/sentence
boundaries. Comparisons and unknown angle-bracket text are preserved; oversized
sentences use an explicit omission marker, or return unknown when the matching
claim cannot fit. This is intentionally not a Markdown/HTML renderer.

Python capability rules recognize a small set of explicit changelog statements.
HTML and Markdown release headings are supported. Attribution requires registry
membership, a release/tag commit, matching versioned package configuration and
matching notes fetched at that commit. Lost capability evidence retains the
previous record as unknown, never deprecated. Mutable usage pages remain documentation
evidence and do not establish introduction versions. No AST analysis or cross-SDK
parity is inferred. Skill content and SDK configuration are fetched at resolved
commits to avoid mixing a moving branch's body with an earlier commit hash.

New limitation/cookbook/evaluation-tool layers establish tracking baselines rather than inventing
historical additions. Subsequent additions, modifications and navigation removals
use the existing stable event IDs. Model limitation absence changes status to
unknown. Source hashes and review dates alone do not create limitation or cookbook
semantic events; they remain provenance updates.

## Runtime and failed attempts

`sync --diagnostics PATH` writes bounded per-attempt metadata outside canonical
state. The workflow adds its failure phase and checkout SHA and uploads that file
on failure, including validation or push failures after successful observation.
It excludes raw exceptions, bodies, credentials and machine paths. Available
fields reflect the last completed phase; missing counts are unknown, never zero.
The exact tested uv version is shared through `tool.uv.required-version`, and
workflow installs use the lockfile. Actions retain immutable SHA pins on stable
Node 24 releases. Compilation is checked in both CI and scheduled synchronization.
