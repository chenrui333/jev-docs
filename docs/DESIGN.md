# Synchronizer design

## Evidence boundary

The canonical web discovery input is `https://docs.typesafe.ai/llms.txt`.
The synchronizer follows HTTPS same-host links from that index and records the public
sitemap's `lastmod` values when available. If the canonical index is unavailable, the run fails and preserves old state.
A working sitemap cannot establish removals from a failed canonical index; it is
not a discovery fallback. Mintlify's `.md` representation is used for Markdown
fetches. The normal page form is used only when the Markdown URL returns 404; a
transient Markdown failure fails the run instead of recording different bytes.
Redirect targets must retain the HTTPS origin and contain no credentials. The
urllib redirect handler rejects foreign origins **before** sending a request, not
merely after downloading their response.

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
observation timestamp and sitemap `lastmod` while its content hash is unchanged.
This avoids clock- and redeploy-driven churn while retaining meaningful
observation timestamps. `state/sources.json` and the per-family `sources/`
manifests share one record per artifact. A first-ever transition keeps the legacy
SHA-256 identity over schema, entity, change type, and before/after hashes.
Subsequent transitions are identified by that legacy base ID plus their latest
persisted predecessor ID, scoped by `(category, entity)`. New records retain
`previous_event_id` for audit; existing historical IDs and records are untouched.
An A→B→A→B sequence therefore records all three transitions. A candidate with
the same change type and before/after hashes as its latest event reuses that ID,
so replay after events were persisted ahead of state does not invent a recurrence.
No-op observations produce no candidate. Observation timestamps never enter an ID.
Predecessor links order new chains even if clocks move backwards; unchained
legacy records use `observed_at`, then sorted filename and stored record order
as a stable tie-breaker.

## Practices

V1 uses an explicit table of conservative extraction rules in
`src/jev_docs/sync.py`. Each rule has a stable ID, category, status, summary,
and one or more source patterns. A rule becomes `recommended` only when current
canonical text matches; absent evidence becomes `unknown`, not deprecated.
Matching excerpts are retained in the derived practice item so a reader can
audit the interpretation without downloading the full source.

## Failure and freshness semantics

Discovery and required source fetches complete before publication. A transport,
parse or tree-integrity failure exits nonzero without publishing new evidence.
Successful discovery can establish navigation removals; failed discovery cannot.
SDK release-reconciliation health is separate: an inconsistent candidate cannot
overwrite an existing healthy SDK snapshot, even during its grace period.

While an SDK is unhealthy, its previous state and exact retained source records stay
together. Candidate source records have `candidate:` identities and are referenced by
freshness `candidate_provenance`, so current registry observations never overwrite
the hashes supporting the retained SDK. Freshness records both candidate and retained
versions. Without a healthy prior snapshot, explicitly unresolved bootstrap state
remains unknown rather than inventing a release.
Mutable changelog citations are retained under hash-scoped `retained:` source IDs
with `historical-documentation` records; current navigation keeps its newly fetched
hash. Retained release-note pointers are rewritten only to those explicit historical
identities, without changing their claims. Strict validation checks nested changelog
citations as well as top-level SDK evidence.

Each SDK keeps `discrepancy_first_observed` throughout a continuous discrepancy.
After three days strict sync still exits nonzero and reports every expired SDK,
but completes validation and publication of independent valid evidence first.
Diagnostics set `publication_eligible=true` only after the full tree is validated
and promoted, with `phase=sync-complete` and explicit `health_errors`. Publication
automation may commit this consistent tree while the workflow remains failed for
SDK health. Ordinary fetch/parse/integrity failures never grant that eligibility.
Repeated unhealthy observations without source changes are byte-idempotent.
Convergence clears the discrepancy and advances the release state normally.

Public HTTP requests use a 20-second timeout and two retries (three total
attempts). Transport failures, HTTP 5xx and 429 retry with bounded backoff.
GitHub 403 retries only when API headers explicitly show exhausted primary
quota (`X-RateLimit-Remaining: 0`) or a valid `Retry-After` secondary-limit signal;
ordinary forbidden responses fail immediately. Numeric and HTTP-date
`Retry-After` values, and primary quota reset times, cap each wait at 60 seconds.
Missing or invalid delay headers cannot expand the retry count. Errors expose
safe source labels, HTTP status and `FetchError.code`, never response bodies,
credentials, query parameters or transport exception chains.

`GITHUB_TOKEN` is optional and sent only to the exact HTTPS `api.github.com`
origin (default port 443). It is not sent to registries, raw source hosts or
redirected requests, including same-origin redirects. Responses are bounded to
16 MiB before JSON decoding; `HTTPClient(max_response_bytes=...)` permits a
positive test/application-specific bound. Injected opener responses implement
`read(limit)` so fixtures exercise the same bounded-read contract.

PyPI metadata must be an object with a nonempty `info.version` string and a
`releases` version-to-file-list map containing that exact published version.
npm metadata must be an object with a nonempty `dist-tags.latest` string.
Invalid JSON and malformed registry, SDK
configuration or consumed GitHub response shapes fail closed with
`invalid_json`/`invalid_schema` errors identifying the source and expected field,
without promoting state.
GitHub head and skill commit responses must provide 40-hex commit SHAs; branch
names, relative paths and malformed hashes cannot identify immutable evidence.

GitHub SDK and evaluation-tool tag/release lists follow `Link` pagination only
within the exact API origin, repository and endpoint path. At most 20 pages are
read; a remaining next link or cycle fails the observation rather than silently
truncating evidence. Projected entries are deduplicated and canonically ordered,
including the `draft` and `prerelease` decision flags (defaulting to false when
absent). Changing either flag changes the evidence hash, and conflicting flag
records survive pagination deduplication; volatile counters remain excluded.

### Interrupted promotion

Synchronization holds an exclusive, nonblocking POSIX `flock` on the ignored
`.jev-docs.lock` for recovery, observation and promotion. An overlapping local
synchronizer fails before reading its baseline or fetching sources; process exit
releases the lock. The lock file remains in place to preserve lock identity.

After staged validation, the synchronizer writes immutable candidate bytes,
checksums and an exact generated-path manifest into ignored `.jev-docs-pending/`.
Files and directories are flushed before writing the final `READY` marker.
Promotion copies from those retained candidates and atomically replaces individual
targets: daily reports first, their event files next, state and source snapshots
afterwards, and the first-bootstrap marker last. After all targets are flushed,
`READY` is durably removed before journal cleanup.

On restart, synchronization rolls a ready batch forward **before** reading state
or observing upstream again. Recovery keeps the original event dates, IDs and
bytes even across midnight or when the next upstream observation differs.
Incomplete journal preparation or cleanup without `READY` is safely discarded.
All journal paths are allowlisted and every candidate checksum and path is checked
before any replacement; symlinks are rejected. Validation refuses a pending
transaction explicitly: rerun `just sync` to recover it rather than deleting the
journal. A failed new fetch can therefore follow successful recovery of an older,
already validated observation.

After recovery, an established baseline passes strict validation of the complete
retained tree before the next observation loads its baseline or fetches sources. Duplicate
IDs across older event files, inconsistent historical reports or other existing
tree-integrity failures abort the run; validating only the new stage cannot hide
them. This preflight also applies to non-strict synchronization.

Strict history validation also requires every non-null predecessor to exist in
the same `(category, entity)` group, permits at most one successor per event, and
rejects cycles. Multiple unlinked legacy roots remain valid; no historical links
are invented. These checks also apply to `validate --strict`.

Staged validation uses the retained history as context, overlaying staged event
and report files by day. A current-day rollup therefore replaces that day's
validation view instead of duplicating its old records, while predecessors from
earlier days remain available. The complete merged graph is checked before
promotion without copying historical files into the publication batch.

This is recoverable multi-file promotion, not an instantaneous snapshot for
readers that ignore the lock. Interrupted files can be temporarily inconsistent
until recovery; validation/publication must wait for completion. Missing or
corrupted journal candidates fail closed and need operator repair. The
synchronizer does not reconstruct gaps from pre-journal interruptions or
transitions already omitted by legacy deduplication, and never rewrites history
to guess them.

## Deferred work

V1 does not reconstruct a mutable hosted-doc timeline, mirror full pages, run
AST compatibility analysis, ingest community material, or infer deprecation from
absence. Those features require stronger evidence and should remain separate
from the canonical layer.

## Structured official guidance

`state/model-limitations.json` uses IDs scoped by the page's explicit model
applicability and section anchor. Second- and third-level headings retain the
upstream distinctions, including numeric/counting/score subtopics. New summaries
are labeled repository interpretations, selected only for an exact reviewed
model, section anchor, and complete section hash. New or changed evidence has
an explicitly unknown summary until reviewed; familiar headings alone are not
evidence of unchanged meaning. No paid inference is used. Mitigation prose is
not copied (the legacy field remains null); source URLs, artifact hashes and
original normalized section hashes remain available for audit.
The additive `complete_section_sha256` hashes the entire section's Markdown with
whitespace normalized, including fenced examples, Info/Tip callouts and trailing
prose. It controls interpretation matching and semantic changes. The original
`section_text_sha256` retains its legacy trimmed/cleaned semantics for audit;
absent historical sections keep their original hashes and unknown status.
Missing sections or pages retain last-known evidence with `unknown` status, not a claim of a fix.
A later model receives independent IDs and cannot inherit an interpretation.
`MODEL_LIMITATIONS.md` deterministically renders new interpretations when the
additive `summary_kind` marker is present. Legacy state without that marker keeps
its historical rendering until synchronization upgrades it, preserving validation
of the recorded tree. Historical state and events are not rewritten. Hosted-
document licensing remains uncertain; the repository's MIT license does not
authorize reproduction of upstream documentation.

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
The first migration of a legacy limitation item to `summary_kind` emits a
per-item baseline explicitly attributed to the local interpretation/quotation
policy only when its ID, model, legacy section hash, and status are unchanged.
Adding a complete-section hash also requires identical source artifact hashes
to qualify as a local baseline; an already-recorded complete hash must match.
Concurrent evidence or status changes remain modifications, explicitly noting both the
observed change and local quotation-policy migration. Later section-hash or
status changes remain modifications; identical subsequent observations emit
no event.

Practice and primitive items come from fixed rule tables in this repository.
When a rule is added or removed, the item's event uses `baseline` or `untracked`
rather than `added` or `removed`. That event records a change in what this
repository tracks, not upstream history. Status and summary changes to an
existing rule remain `modified`.

## Runtime and failed attempts

`sync --diagnostics PATH` writes bounded per-attempt metadata outside canonical
state. The workflow adds its failure phase and checkout SHA and uploads that file
on failure, including validation or push failures after successful observation.
It excludes raw exceptions, bodies, credentials and machine paths. Available
fields reflect the last completed phase; missing counts are unknown, never zero.
The exact tested uv version is shared through `tool.uv.required-version`, and
workflow installs use the lockfile. Actions retain immutable SHA pins on stable
Node 24 releases. Compilation is checked in both CI and scheduled synchronization.
