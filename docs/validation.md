# Claim validation

`app/modules/rag/validation.py` is step 8 of the answer pipeline (`RAGService.answer_events`). The model's JSON
answer is untrusted until it passes. Every claim is checked against the passages it cites. A claim that fails is
removed, never silently "fixed". If no claim survives, the answer becomes a no-answer
(`INSUFFICIENT_EVIDENCE` or `ANSWER_FAILED_VALIDATION`).

```
model JSON ──► checked_calculations (calculate.py)
           ──► validate_claims ──► ClaimResult[]  (valid / wording_only / problems)
           ──► _drop_metadata_echo
           ──► _judge (meaning check, wording-only claims only)
           ──► _verify_citations (ACL)
           ──► _check_on_topic ──► summary
```

## Entry point

```python
validate_claims(
    raw_claims: list[dict],                 # [{"text": ..., "evidence_ids": ["E1", ...]}, ...]
    evidence: dict[str, EvidenceText],      # the passages the model saw, by id
    subjects: list[str] | None = None,      # key_terms(question): what the claims must be about
    question: str = "",                     # read for variants, the reader's figures, premises
    calculations: list[Calculation] | None = None,  # recomputed arithmetic from calculate.py
) -> list[ClaimResult]
```

Called from `service.py` in three places:

| Caller | What it validates |
|---|---|
| Main answer path | All claims of an answer, with `checked_calculations`. Then `_judge`, ACL and on-topic checks |
| `_streamed_claim` | One claim as it streams, checked exactly as the final answer checks it |
| `_summary` | The plain-words summary, citing its claims' evidence. A wording-only failure goes on to the summary meaning check; a hard failure drops the summary (never repaired) |

### Inputs

`EvidenceText` (one per passage):

| Field | Meaning |
|---|---|
| `id` | `E1`, `G2`, `D1` ... |
| `text` | Passage text including expanded context |
| `allowed_numbers` | Provenance figures (section number, version, pages) that a claim may repeat |
| `label` | Policy name, document title, section heading. A passage is about its policy even where it doesn't repeat the name |
| `versions` | The version(s) the passage is from (two for a diff) |

### Output

`ClaimResult`:

| Field | Meaning |
|---|---|
| `text` | The claim, with trailing citation marks (`(E1, E2)`, `[E3]`, a dangling `[`) stripped |
| `evidence_ids` | The cited ids that exist |
| `valid` | Passed every check |
| `problems` | Why it failed (goes to the audit trail, `plan.checks`, and the retry prompt) |
| `numbers_checked` | How many figures were examined |
| `wording_only` | Failed, but only on wording, so `RAGService._judge` may rescue it by meaning |
| `arithmetic` | States a figure produced by arithmetic (derived or recomputed) |

## The checks, in order

Each claim runs through these in sequence. Some return early.

### 1. Absence statements (hard, early exit)

`_ABSENCE` matches statements about what the evidence lacks:

- "The document does not (explicitly) provide/mention/specify ..."
- "... is not stated in the policy"
- "No information is provided ..."
- "There is no stated ..."
- "in the provided evidence / documents"

A citation can't show an absence, so the claim fails with `ABSENCE_PROBLEM` ("says what the documents do not
contain"). `service.py` turns this into one warning: "Part of your question is not covered by your documents."

### 2. Citations (hard, early exit)

- Ids not in `evidence` are reported as `invented citation(s) ...`.
- With no known id left, the result is `no valid citation` and the claim fails.

### 3. Self-reference (hard, early exit)

A claim that talks about evidence ids ("E2 states 'Chapter : Contents'") describes the evidence, not the
document. It fails unless that id string actually appears in the cited text.

### 4. Figures (`numbers_checked`)

Every figure in the claim is extracted with `extract_numeric_facts` (rates, amounts, percentages, dates, tenures,
numbers). Version references that name a real evidence version ("Version 2.0") are skipped. Each figure must be
one of the following.

| Accepted as | How |
|---|---|
| **Stated** | Its key or value is in the cited text, its `allowed_numbers`, or a range start ("28 to 65 years" gives 28 its unit). A bare value also matches with a unit ("75" for "75%", "60" for "60-day") |
| **Derived** (`_derived`) | One step of arithmetic on the question's figures and the evidence's: rate × amount (only when the documents state one of them), sum, difference, product, ratio, share ("Rs 35,000 is 58.33% of Rs 60,000"), EMI × months over the question's years. Also the evidence's duration in another unit ("72 months" → "6 years") |
| **Carried** (`_from_carried`) | One step on a result an earlier valid claim of the same answer established ("50% of Rs 1,20,000 is Rs 60,000", then "Rs 60,000 − Rs 25,000 = Rs 35,000") |
| **Computed** | Matches a `Calculation` recomputed over the evidence this claim cites (`matches`), or an equation the claim writes itself (`written_arithmetic` → `states_value`), at the precision written. "about/approximately" allows 0.5% |
| **The reader's figure** | A figure of the question, used the way the question uses it (`_given_in_context`), beside at least one figure that passed above |

Arithmetic on the evidence's figures alone is never accepted (except a change of unit). With enough figures in
a passage, some sum would "explain" almost any invented number. A result equal to one of its operands derives
nothing.

**The reader's figure in context** (`_given_in_context`) means one of these:
- after a preposition or contrast: "for a Rs 60 lakh loan", "aged 27", "not 1%", "than ...";
- before a comparison: "27, which is below ...", "years old, which is under ...", "is not eligible", "meets";
- after "you/your": "Your total EMIs are 60%".

The reader's figures also match restated ("I am 27" → "27 years", "22 years old" → "an age of 22").
`without_version_refs` keeps "Version 6" out of them.

**Figure failures:**

| Problem | Hard? | Why |
|---|---|---|
| `the calculation X is wrong` (`wrong_arithmetic`) | Yes | A false equation is false however true each figure is. "650 - 699" is a range, not subtraction |
| `'2%' is the question's claim about the documents, not their figure` | Yes | `premise_values`: a figure the question attributes to the documents ("the policy says the rate is 2%", "Policy update: ...", "according to ..."). It may only be corrected ("10.05%, not 2%") |
| `'X' is the question's figure, not set against a rule` | **No** (wording-only) | The reader's figure phrased in a way the patterns don't recognise. Whether it is presented as theirs is a question of meaning |
| `'X' is not in the cited evidence` | Yes | Invented |

### 5. Word support (wording-only)

The claim's content words are those of 3+ letters, minus `_STOP` and `_FRAMING`. They are looked up in the cited
text plus the passages' labels through `TermIndex` (stems, word forms, synonyms, abbreviations). At least
`MIN_SUPPORT = 0.35` of them must be found.

- `_FRAMING` words carry no fact: reporting words ("states", "according", "listed"), the reader's outcome
  ("qualify", "eligible", "meets", "threshold", "pass/fail") and table-row words ("falls", "band", "bracket",
  "slab", "tier").
- When the claim states a derived or computed figure, `_RESULT_WORDS` ("total", "save", "difference"),
  `_ADDRESSING` ("you would", "about") and the question's own words also count as supported.

Failure: `weak support (N% of terms found in evidence)`. This is wording-only.

0.35 rather than 0.5 is deliberate. A faithful paraphrase shares most but not all of its vocabulary. The figure
check is the load-bearing guard against fabrication, and word overlap is the coarse net.

### 6. Subjects (wording-only)

`subjects` (the question's key terms) that the claim mentions must also be mentioned by the cited evidence or
its labels. Function words and figures are not subjects (`_is_subject`). Result words are exempt when the claim
states a computed figure.

Failure: `the cited evidence does not mention X`. This is wording-only. Example: a claim saying "criterion" of a
table that never uses the word.

### 7. Number binding (hard)

Checked only if the subjects passed. `_unbound_numbers` catches a figure taken from another row of a table:

1. It applies only when the cited text has at least `MIN_FIGURES_FOR_ROWS = 3` value figures. A clause with one
   or two figures has no rows to mix up. Bare clause numbers aren't values unless a measure word precedes them
   ("threshold 17.8").
2. Each figure's window (`_window`) is its sentence within its line, at least `NUMBER_WINDOW = 200` characters
   either side. A table cell also gets its column heading and the first column's heading (`_column_heading`).
3. A subject beside more than `SPECIFIC_SHARE = 0.5` of the figures is a column heading and identifies no row.
   The specific subjects remain.
4. A claim figure whose every occurrence in the evidence sits away from all specific subjects is unbound.

Failure: `10 Years is not stated for MAP/SIR Reports in the cited evidence`.

A claim that puts several of the reader's figures in one sentence often fails here, because they can't be tied
to their rows. The retry is told to write one claim per figure.

### 8. Variants (hard)

`qualifiers(question)` reads the variant the question pins down: tier, level, grade, band, category, class,
phase, stage, slab, bucket + a number or letter, and "X Zone". A claim about another value of the same variant
fails: `is about Tier 3, not Tier 2`. Section numbers are not variants.

### 9. Versions (hard)

`_unsupported_versions`:

| Claim | Requirement |
|---|---|
| Names "Version 1.0" | Cites a Version 1.0 passage, or a passage whose text names it |
| "both / each / every / all versions" | Cites at least two versions, and each version's passage mentions the subject |
| "... but not in Version 2.0" | No single-version passage of Version 2.0 in the evidence mentions the subject. A citation can't show an absence, so the evidence is searched |

### 10. Polarity (hard)

`_flips_polarity` checks a claim that restates one source sentence (`RESTATEMENT_OVERLAP = 0.7` of its content
words). It must keep that sentence's negation.

- Before comparing, a contrast that corrects a figure the source doesn't state (", not 90 days", "rather than
  1%") is removed, and so is a leading answer word ("No, the minimum ... is 700").
- **Dropped negation**: always a failure. "No prepayment charges apply" must not become "Prepayment charges
  apply", provided the claim repeats the negated part (`_restates_negated_part`), not another part of the sentence.
- **Added negation**: allowed when
  - it's the reader's outcome (`outcome=True`: the claim applies the reader's figure, "At 27, you do not meet the
    age of 28 to 65"), or
  - it negates something the sentence doesn't say ("Life insurance is not mandatory" of "... is optional"), or
  - the negation is quoted from another cited sentence (`_negations_quoted`), or
  - the next sentences negate (a Q&A pair: "... income? Ans. No.").
- "Sl. No.", "No. of", "No Change" are not negations (`_NOT_NEGATION`).

Failure: `reverses the negation of the source sentence`.

## Hard failures vs wording-only

```python
result.wording_only = not result.valid and not hard
```

| Hard: never judged by meaning | Wording-only: the meaning check may keep it |
|---|---|
| Absence statement | Weak word support |
| No valid / invented citation, evidence-id talk | A question word the passage doesn't use (subject check) |
| Figure in neither evidence nor question | The reader's figure phrased unrecognisably |
| Wrong arithmetic | |
| The question's premise stated as fact | |
| Figure from another row | |
| Wrong tier / variant | |
| Wrong version | |
| Dropped "not" | |

In `service.py`, wording-only claims go to `RAGService._judge` (`MEANING_CHECK_PROMPT`): one model call with the
question and each claim's passage. Only claims judged both `supported` and `answers` are kept. With no verdict
(`RAG_MEANING_CHECK=False`, model down, local stand-in), they stay removed. The check fails closed.

## `TermIndex`: does a text mention a term?

Shared by validation, `evidence.py` and `service.py`.

- Lowercases, joins "FIU- IND" broken at a hyphen, reads "%" as "percent", and turns limit phrases into words
  ("up to" → `upto`, "not more than" → `atmost`, "not less than" → `least`).
- Clause numbers count for their section too ("4.25.9" also counts as "4.25").
- Words are stemmed. Stems sharing `TERM_PREFIX = 5` leading characters and at least `TERM_PREFIX_SHARE = 0.7` of
  the shorter stem are one word ("generate"/"generation", but not "customer"/"custodian"). Numbers never join by
  prefix.

`mentions(term)` is true if every part of the term (split on `-` and `/`) is present directly, through a
`SYNONYM_GROUPS` stem, or, for a 2-6 letter part, as the initials of a run of capitalised words in the source
(`capitalised_initials`: "Non-Performing Asset" → `npa`, "Loan-to-Value" → `ltv`, "Fixed Obligation to Income
Ratio" → `foir`). `count(term)` gives the count of the rarest part. Build one index and call `mentions_all`
rather than one index per term.

`SYNONYM_GROUPS` holds **general English and lending synonyms only** (own/owner/custodian,
retain/preserve/keep, maximum/cap/ceiling/upto, fee/charge/levy, emi/instalment ...). They only decide whether
a term is *mentioned*. They never put a number or a name into an answer. Never add per-tenant or per-document
words. Aliases come from the document data (abbreviations from capitalised runs).

## Constants

| Name | Value | Purpose |
|---|---|---|
| `MIN_SUPPORT` | 0.35 | Share of content words the evidence must contain |
| `TERM_PREFIX` | 5 | Shared stem prefix for word forms |
| `TERM_PREFIX_SHARE` | 0.7 | ... and share of the shorter stem |
| `NUMBER_WINDOW` | 200 | Characters either side that count as "beside" a figure |
| `SPECIFIC_SHARE` | 0.5 | A subject beside more figures than this is a column heading |
| `MIN_FIGURES_FOR_ROWS` | 3 | Fewer figures: a clause, no row binding |
| `RESTATEMENT_OVERLAP` | 0.7 | Overlap above which polarity is compared |
| `MIN_ABBREVIATION`, `MAX_ABBREVIATION` | 2, 6 | Abbreviation length for initials matching |
| `ABSENCE_PROBLEM` | "says what the documents do not contain" | Problem string `service.py` looks for |

## Public helpers used elsewhere

| Name | Used for |
|---|---|
| `validate_claims`, `ClaimResult`, `EvidenceText` | The check itself |
| `TermIndex`, `term_parts`, `capitalised_initials` | Term matching across RAG |
| `qualifiers` | Variants the question pins down |
| `premise_values` | Figures the question attributes to the documents (also used by the prompt rules) |
| `SYNONYM_GROUPS` | General synonyms |
| `ABSENCE_PROBLEM` | Detecting the "not covered" warning |

## Where failures show up

- **Audit trail.** `plan.checks` of the answer or the no-answer (`audit_events`, `action='ai.query'`):
  - "Removed an unsupported statement: ..."
  - "Kept after a meaning check (...): ..."
  - "Ignored invented citation(s) ..." (on a claim that survived with other valid ids)

  Each attempt is listed ("Attempt 2: ..."). Read these instead of re-running a UI question.
- **Retry.** When nothing survives, `_reasoning_feedback` tells the next attempt what removed each claim
  (“... was removed: weak support (20% ...)”) so it is not written the same way again.

## Changing validation safely

- Fix the rule for a type of question, not a word list. When a correct answer is rejected for its wording, it
  should become `wording_only` (judged by meaning), not pass a loosened check.
- Never move a hard failure to wording-only, and never loosen a figure, citation, version, variant or negation
  check in a way that lets an unsupported answer through.
- When answer behaviour changes, bump `ANSWER_CACHE_VERSION` in `app/modules/rag/service.py` with a comment line.
  Cached answers live 10 minutes.
- Unit tests: `app/tests/unit/test_rag_validation.py` (local providers, `<db>_test`, no OpenAI calls). Ask before
  running them.
