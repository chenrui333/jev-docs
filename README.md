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

API field claims use explicit canonical reference declarations, not examples or
link metadata. Endpoint declarations preserve the reference's explicit HTTP
fence while excluding inactive containers. Unsupported attribute syntax or an
unknown question-type declaration leaves the affected claim unknown with a warning
rather than verifying a partial
list. Conflicting requiredness does not erase independently known request field
names. Response key correlation is an explicitly reviewed repository interpretation,
not automatic prose extraction: it applies only to the complete reviewed Response
body section SHA-256, including examples. An unreviewed context stays unknown even
when familiar affirmative fragments remain. Properties record the reviewed and
observed section hashes, method, interpretation label, and bounded verbatim evidence.
Declarations exceeding the 240-character evidence bound leave affected claims
unknown rather than clipping away their proof. A required model's example alias
is not a default, and correcting local assumptions is not an upstream history event.
If canonical source bytes also changed during that migration, the API baseline
records the first verified snapshot without inferring historical API behavior;
documentation source events independently record the changed bytes.
Loss or recovery of API verification emits `verification_changed`, not a contract
modification; retained earlier citations have an explicit `historical` role.
Verified-to-verified fact changes still emit `modified`.

Practice statuses are `recommended`, `discouraged`, `anti_pattern`,
`deprecated`, or `unknown`. A practice disappearing from one page is not marked
deprecated automatically: removal requires explicit evidence, so a failed or
partial source fetch cannot erase a recommendation.

Practice recommendations require two independent checks: complete literal claim
bundles from one permitted source, and a manual review of that source's complete
context. [`practice_reviews.py`](src/jev_docs/practice_reviews.py) records only
source URLs, hashes, and explicit per-practice `supported`, `non_conflicting`, or
`rejected` review verdicts. No new context or rule/source pair is approved by default.
Any fetched unreviewed or conflicting context leaves the practice `unknown`, even
if another source still supports it. This deliberately favors false negatives over
an expanding, incomplete natural-language rejection-keyword list.

The review signature preserves the complete source, including markup, quotations,
and examples; it normalizes line endings, trailing whitespace, and runs of blank
lines only. Code examples are excluded separately when checking literal supporting
passages. A changed context requires an evidence-backed review before a
recommendation can resume. Review-only transitions are `verification_changed`, not
upstream guidance introductions or deprecations.
Current recommended evidence and recorded source observations must match the
source IDs and SHA-256 hashes retained in `state/sources.json`. Historical
evidence for unknown practices is exempt from this current-artifact check;
missing optional sources need not appear in the observation map.
The first stricter-policy observation is a tracking-only baseline only when
every allowed source for that rule is provably unchanged, including previously
neutral sources. Changed or unknown source revisions remain ordinary transitions.

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
check` validates generated JSON, provenance, stored canonical fingerprints, and
agreement between coverage, family manifests, and `state/sources.json`. Coverage
schema 2 requires the complete generated state, both guides, and both manifests;
schema 1 remains readable for migration. Missing or unsupported coverage versions
fail before source collection; neither can select legacy compatibility.
Semantic state snapshots require their
stored fingerprints; only the source inventory and coverage files are exempt.
Present family manifests must match the complete source inventory even when their
artifact lists are empty; empty discovery cannot claim complete coverage.
Source envelopes also require the recognized inventory schema, official family
URLs, and unchanged aggregate scope/licensing notice; exemption from fingerprints
does not permit relabeling the evidence.
Source locations must match official identities: TypeSafe HTTPS documentation,
the named GitHub repository/API endpoint, commit-addressed raw files, or the
specific PyPI/npm package. Candidate records retain these same requirements.
Hosted pages, registry responses, and GitHub API observations cannot claim a Git
commit/release tag; commit-only skill observations cannot acquire a release tag.
Tagged raw files must agree with retained SDK/evaluation release tag/commit
bindings. Held candidates record observed tag bindings without publishing SDK
claims. Legacy held candidates remain explicitly unverified until recollection.
The raw skill artifact and GitHub commit observation must match the retained skill
snapshot's hash, immutable URL, and commit; deleting either observation fails validation.
SDK head observation hashes bind to `main_commit`, never the published release's
`source_commit`; held candidate hashes retain their existing protected freshness references.
SDK/evaluation snapshot citations must match retained source IDs, hashes, and fetch
URLs, including legacy untagged SDK configurations; URL shape alone is insufficient.
Fingerprint validation excludes top-level observation timestamps and freshness-only
observation/change footers. Legacy family-manifest observation timestamps may differ
without rewriting old records; all semantic source metadata must agree. Corrupt
persisted JSON is reported by
relative filename without exposing its contents or local machine path.
Persisted and upstream JSON allow at most 64 nested containers, independent of the
Python runtime; brackets inside strings do not count toward this bound.
Coverage retains requested navigation identities even when a page redirects to a
different same-origin HTTPS path; manifests retain the resolved fetch location.
`just check-strict` additionally requires event IDs to be unique across all
history, each `changes/` report to be the rendering of its `events/` file, and
`BEST_PRACTICES.md` to match its state.

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
