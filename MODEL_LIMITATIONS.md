# Model-specific Jev limitations

Upstream-documented limitations for specific model versions; absence does not establish a fix.

Generated from [structured state](state/model-limitations.json). These describe the named version, not all present or future Jev models.

## jev-1.13 / Adversarial content

Status: `documented`. Upstream last reviewed: 2026-10-02.

State is data, and jev-1.13 does not treat it as hostile by default.

Upstream mitigation: be explicit in the criteria. Test your integration thoroughly before deploying it to many users.

[Official evidence](https://docs.typesafe.ai/model-jaggedness/jev-1.13.md#adversarial-content)

## jev-1.13 / Choice option order

Status: `documented`. Upstream last reviewed: 2026-10-02.

In some cases, we observed that the order of a Choice's options can affect the answer, and jev-1.13 leans toward the option that comes first.

Upstream mitigation: reorder the options to double check that the answer stays consistent.

[Official evidence](https://docs.typesafe.ai/model-jaggedness/jev-1.13.md#choice-option-order)

## jev-1.13 / Common-sense structural invariants

Status: `unknown`. Upstream last reviewed: 2026-09-17.

jev-1.13 is extremely consistent, meaning you should expect quantitatively similar outputs for semantically similar inputs. However there are many structural invariants one might imagine to hold that simply aren't guaranteed by the model.

Upstream mitigation: don't rely on expected structural invariance, and word questions to mean directly what you want. Don't carry a threshold tuned on a Noul over to a Choice, and don't hold the model to arithmetic identities between separate questions. A Choice over options and one Noul per option answer different questions: the Choice is relative, settling which option, while each Noul is absolute and can be low for all of them. The skill suggestion cookbook uses both on the same shortlist, the Choice to pick a skill and the Nouls to decide whether to suggest one at all.

[Official evidence](https://docs.typesafe.ai/model-jaggedness/jev-1.13.md#common-sense-structural-invariants)

## jev-1.13 / Contradictory instructions and criteria

Status: `documented`. Upstream last reviewed: 2026-10-02.

When the instructions and the criteria ask for different things, jev-1.13 might get confused.

Upstream mitigation: treat the criteria as an extension of the instruction. Align the two using clear and precise language.

[Official evidence](https://docs.typesafe.ai/model-jaggedness/jev-1.13.md#contradictory-instructions-and-criteria)

## jev-1.13 / Counting

Status: `documented`. Upstream last reviewed: 2026-10-02.

jev-1.13 does not count reliably. This covers characters in a word, occurrences of a term in a passage, and items in a long list. The model recognizes the shape of an answer rather than tallying, and the error grows with the size of the thing being counted.

Upstream mitigation: count in code. When you want to count items matching some criteria, iterate in code over the candidates and ask one question for each, then add up the answers yourself.

[Official evidence](https://docs.typesafe.ai/model-jaggedness/jev-1.13.md#counting)

## jev-1.13 / Date and time comparison

Status: `documented`. Upstream last reviewed: 2026-10-02.

jev-1.13 reads dates as text, not as ordered quantities.

Upstream mitigation: split the work. Extraction is a judgment, so give it to the model. Arithmetic is not, so keep it in code.

[Official evidence](https://docs.typesafe.ai/model-jaggedness/jev-1.13.md#date-and-time-comparison)

## jev-1.13 / Generation

Status: `documented`. Upstream last reviewed: 2026-10-02.

jev-1.13 is not trained to generate text. While you can force it to by chaining choices, this will not work well and will be very slow. For data extraction, it is better to extract possible options using regex or a generative model and let jev-1.13 pick the correct extraction.

Upstream mitigation: when the answer space is bounded, turn extraction into a Choice over the options rather than asking for the value itself. If you really need to generate text... there are other models for that.

[Official evidence](https://docs.typesafe.ai/model-jaggedness/jev-1.13.md#generation)

## jev-1.13 / Indirection

Status: `documented`. Upstream last reviewed: 2026-10-02.

Instructions carrying double negatives or complex indirection are answered less reliably. A question about a property of a property or something that requires multiple hops of reasoning costs accuracy.

Upstream mitigation: write your instructions as directly as possible. When possible, identify the relevant parts of state by name.

[Official evidence](https://docs.typesafe.ai/model-jaggedness/jev-1.13.md#indirection)

## jev-1.13 / Large state full of irrelevant detail

Status: `documented`. Upstream last reviewed: 2026-10-02.

Accuracy falls as the state grows with content unrelated to the decision. Unrelated detail acts as a distractor, and a large state makes it harder to tell which part of the input produced a wrong answer.

Upstream mitigation: retrieve and filter in code first, and send only the fields the question needs. When it's not possible to filter in state, you can use a Noul to filter for relevance. The classifying RAG passages cookbook has a worked example.

[Official evidence](https://docs.typesafe.ai/model-jaggedness/jev-1.13.md#large-state-full-of-irrelevant-detail)

## jev-1.13 / Literal reading

Status: `documented`. Upstream last reviewed: 2026-10-02.

jev-1.13 answers the question you wrote, not the one you meant. Scoping words, negations, and implied conditions are read at face value. A question will be answered based on the words written in the instruction, whereas a person might have read the intent behind the instructions.

Upstream mitigation: state the exact condition in the instructions. Be specific. Put boundary cases in the criteria. When you look at a wrong answer and find yourself explaining what you really meant, that explanation is the missing half of the instruction. Where interpretation is unavoidable, split it into two literal questions and combine them in code.

[Official evidence](https://docs.typesafe.ai/model-jaggedness/jev-1.13.md#literal-reading)

## jev-1.13 / Math and Numbers

Status: `documented`. Upstream last reviewed: 2026-10-02.

Jev is not a calculator. We strongly recommend implementing any mathematical logic in code. Jev will perform better on semantic questions than mathematical ones.

[Official evidence](https://docs.typesafe.ai/model-jaggedness/jev-1.13.md#math-and-numbers)

## jev-1.13 / Math using score

Status: `documented`. Upstream last reviewed: 2026-10-02.

Please do not use score outputs (e.g., expectations and probability) to compute the exact magnitude of a number between two levels of a criterion.

[Official evidence](https://docs.typesafe.ai/model-jaggedness/jev-1.13.md#math-using-score)

## jev-1.13 / Numeric representations

Status: `documented`. Upstream last reviewed: 2026-10-02.

jev-1.13 will perform better on semantic representations than numeric. For example, questions about colors using hex values will underperform compared to those using the English names. Given RGB triples or hex values it cannot reliably judge whether two values are near each other.

Upstream mitigation: do the conversion in code and pass in either the computed number or a named bucket. Keep the model for the part that is genuinely a judgment, such as whether a color reads as a warning.

[Official evidence](https://docs.typesafe.ai/model-jaggedness/jev-1.13.md#numeric-representations)
