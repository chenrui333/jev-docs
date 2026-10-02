# Jev Engineering Best Practices

Last verified: 2026-09-26

Generated from [`state/practices.json`](state/practices.json). `recommended` means current canonical evidence matched a transparent extraction rule; `unknown` means this snapshot does not provide enough evidence.

## Agent Integration

### jev-complements-generative-agents — `recommended`

Use coding agents to write software that calls Jev for bounded structured decisions such as routing, classification, scoring and guardrails. Jev does not replace their generative base model or write code or converse.

Evidence:
- [docs:introduction/coding-agents](https://docs.typesafe.ai/introduction/coding-agents.md) — If you found TypeSafe while looking for a model to plug into your coding agent, start here. Jev is not a drop-in replacement for the LLM behind Claude Code, Cursor, opencode, Copilot, Muse Spark, Grok Bot, or similar tools. Instead, you can use your coding agent as usual to write code that uses Jev to make decisions.

### prefer-typed-decisions-over-prompt-parsing — `recommended`

For decisions expressible as TypeSafe's typed questions, consider replacing fragile generative return-JSON prompts with typed decision calls. This guidance does not cover arbitrary text generation or every JSON task.

Evidence:
- [docs:introduction/coding-agents](https://docs.typesafe.ai/introduction/coding-agents.md) — Replace a fragile prompt that asks an LLM to "return JSON" with a call that returns typed values by construction.

## Batching and speculative fan-out

### batch-independent-questions — `recommended`

Ask independent questions over the same state together so the model can evaluate them in parallel and code can compose the answers.

Evidence:
- [docs:patterns/fan-out](https://docs.typesafe.ai/patterns/fan-out.md) — Because TypeSafe supports sending many questions in a single API call, we recommend putting all of the questions your system needs in a single request, and then using code to decide what is relevant after the fact. All questions are evaluated in parallel, so adding more questions usually has little effect on response time.
- [skill:typesafe-ai](https://raw.githubusercontent.com/typesafe-ai/skills/65a39f393687675ce170e6094757de20370365b9/skills/typesafe-ai/SKILL.md) — Ask independent questions over the same state together, including useful speculative questions.

### explicit-speculative-premises — `recommended`

Speculative questions are acceptable in a batch, but their premises must be stated explicitly and code must decide which answers are relevant.

Evidence:
- [docs:patterns/fan-out](https://docs.typesafe.ai/patterns/fan-out.md) — Send many questions in a single call, including speculative ones, and let your code decide what's relevant.
- [skill:typesafe-ai](https://raw.githubusercontent.com/typesafe-ai/skills/65a39f393687675ce170e6094757de20370365b9/skills/typesafe-ai/SKILL.md) — State each speculative premise explicitly; code consumes the applicable answers.

## Confidence

### choice-confidence-concentration — `recommended`

Choice confidence summarizes concentration of the competing option probabilities; it is not a guarantee that the selected answer is correct.

Evidence:
- [docs:confidence](https://docs.typesafe.ai/confidence.md) — This gives 0 at $p = 0.5$ and 1 at $p = 0$ or $p = 1$. It is also the Choice formula below applied to a yes-or-no Choice, so it sits on the same scale as Choice confidence.
- [skill:typesafe-ai](https://raw.githubusercontent.com/typesafe-ai/skills/65a39f393687675ce170e6094757de20370365b9/skills/typesafe-ai/SKILL.md) — Choice/Score confidence summarizes distribution concentration, not overall workflow correctness or permission to act.

### confidence-is-not-permission — `recommended`

Confidence is an uncertainty signal. Keep the permission to act, review, or escalate explicit in application code.

Evidence:
- [docs:confidence](https://docs.typesafe.ai/confidence.md) — The 0.5 confidence floor catches anything the model reports as genuinely uncertain. Above that, the threshold for acting without confirmation is higher for a destructive operation than for a read-only one. Your code encodes the risk tolerance.

## Programming model

### deterministic-before-jev — `recommended`

Keep deterministic rules, calculations, exact lookups, control flow, and side effects in code; use Jev where semantic judgment is needed.

Evidence:
- [docs:concepts/how-to-build-with-system-one](https://docs.typesafe.ai/concepts/how-to-build-with-system-one.md) — Keep control flow, deterministic rules, and side effects in code.

## Primitives

### noul-is-yes-probability — `recommended`

A Noul value is the probability that a yes/no proposition is true; a value near 0.5 means uncertainty between yes and no, not medium intensity.

Evidence:
- [docs:primitives/noul](https://docs.typesafe.ai/primitives/noul.md) — A Noul value runs from 0 to 1, but it's not a scale of the thing you asked about. It is the probability that the answer is yes. If the question is really about degree, the value does not measure the degree. Below, "Is the candidate strong in Python?" is asked about four candidates, next to a Score with four levels: no experience, some familiarity, regular use in a job, deep expertise.

## Question construction

### candidate-must-be-present — `recommended`

Candidate selection questions can only select a value that is actually present in the candidate state or criteria.

Evidence:
- [skill:typesafe-ai](https://raw.githubusercontent.com/typesafe-ai/skills/65a39f393687675ce170e6094757de20370365b9/skills/typesafe-ai/SKILL.md) — Keep the needed answers available. Include a no-match outcome when nothing may fit; use a separate presence judgment when it is independently useful. For source-value selection, check candidate coverage: the model cannot choose an omitted value.

### narrow-coherent-questions — `recommended`

Ask narrow, coherent, typed questions with explicit instructions and criteria rather than hiding several judgments in one broad question.

Evidence:
- [docs:concepts/how-to-build-with-system-one](https://docs.typesafe.ai/concepts/how-to-build-with-system-one.md) — Break broad judgments into narrow, typed questions with explicit instructions and criteria.
- [skill:typesafe-ai](https://raw.githubusercontent.com/typesafe-ai/skills/65a39f393687675ce170e6094757de20370365b9/skills/typesafe-ai/SKILL.md) — Ask one narrow, coherent judgment per question.

## State construction

### observed-state-distinct-from-inference — `recommended`

Keep observed application state and inferred model judgments conceptually distinct, and check freshness before applying a result to changed state.

Evidence:
- [docs:concepts/state](https://docs.typesafe.ai/concepts/state.md) — The state contains the content and supporting facts. Questions define the judgments the model should make about that material. For example, keep the refund request and policy in the state, then ask whether the customer requested a refund and whether the policy supports it.
- [skill:typesafe-ai](https://raw.githubusercontent.com/typesafe-ai/skills/65a39f393687675ce170e6094757de20370365b9/skills/typesafe-ai/SKILL.md) — Respond to changing state. Code can retain goals and observations while fresh judgments guide the next bounded step. Keep inferred state distinct from observed facts, and check freshness before applying a result to a changed situation.

## Thresholds and evaluation

### domain-calibrated-thresholds — `recommended`

Choose thresholds from the application's data, model performance, and consequences; do not treat cookbook thresholds as universal defaults.

Evidence:
- [docs:confidence](https://docs.typesafe.ai/confidence.md) — The correct threshold values depend on your domain and the performance of the model for your use case. Start with conservative thresholds, test with your own data, and adjust as you observe results.
- [skill:typesafe-ai](https://raw.githubusercontent.com/typesafe-ai/skills/65a39f393687675ce170e6094757de20370365b9/skills/typesafe-ai/SKILL.md) — Use probabilities and confidence to guide behavior, with thresholds evaluated on the user's data and consequences.

## Error handling and verification

### diagnose-failure-layers — `recommended`

For failures, inspect the exact state, questions, candidates, answers, composition, and observed outcome; separate evidence, model, code, and service failures.

Evidence:
- [skill:typesafe-ai](https://raw.githubusercontent.com/typesafe-ai/skills/65a39f393687675ce170e6094757de20370365b9/skills/typesafe-ai/SKILL.md) — Test representative cases and the resulting application behavior. For failures, inspect the exact state, questions, candidates, answers, composition, and observed outcome. Separate missing evidence, model errors, code errors, and service failures. Treat cookbook thresholds and demo results as examples to evaluate, not universal rules or permanent model limitations. Keep API credentials server-side in web apps.

### typed-output-is-not-truth — `recommended`

Typed output guarantees interface shape, not truth; test representative cases and resulting application behavior.

Evidence:
- [skill:typesafe-ai](https://raw.githubusercontent.com/typesafe-ai/skills/65a39f393687675ce170e6094757de20370365b9/skills/typesafe-ai/SKILL.md) — Typed output guarantees the interface, not truth.
