# Skill Quality Report Template

Use this shape for the default chat report. Keep it concise unless the user asks for a full audit.

## Summary

State the overall assessed score, SRL level, readiness, confidence, and the one or two issues most likely to affect real use, including any major ambiguity or self-consistency risk.

Example:

`Overall: 78/100, SRL-3, acceptable but not publish-ready. The main risks are weak trigger coverage and no leakage-resistant evals. Assessment confidence is low because no execution transcripts were available.`

Use one of these confidence values:

| Confidence | Meaning |
| --- | --- |
| normal | Enough evidence exists to score the relevant dimensions. |
| low | Evidence is thin but still sufficient for a bounded assessment. |
| insufficient information | Do not assign scores; explain what is missing. |

## Scorecard

| Dimension | Score | Evidence |
| --- | ---: | --- |
| Activation Reliability | n/15 or N/A | Brief rationale |
| Task Coverage and Functional Suitability | n/20 or N/A | Brief rationale |
| Instruction Clarity, Semantic Consistency, and Agent Usability | n/20 or N/A | Brief rationale |
| Context Efficiency and Progressive Disclosure | n/15 or N/A | Brief rationale |
| Safety, Reliability, and Maintainability | n/15 or N/A | Brief rationale |
| Eval Quality, Leakage Resistance, and Real-World Value | n/15 or N/A | Brief rationale |
| Overall Assessed Score | n/m | Exclude N/A dimensions from denominator |
| SRL Level | SRL-1 to SRL-5 or N/A | Reliability level from SRL review |

## Reliability / SRL

Summarize reliability separately from quality score:

| SRL Dimension | Assessment |
| --- | --- |
| Anchor Density | Are key steps backed by CLI/API/DB/files or other external facts? |
| Hallucination Exposure | Which steps rely on unverified AI reasoning? |
| Failure Transparency | Does the skill stop, degrade, or disclose uncertainty when evidence is missing? |
| Traceability | Can users verify the output sources independently? |
| Reproducibility | Would the same input likely produce the same output later? |

State use constraints. Low SRL should lower publish-readiness or require manual review even when the quality score is high.

## Critical Findings

| Priority | Finding | Severity | Evidence Level | Evidence | Impact |
| ---: | --- | --- | --- | --- | --- |
| 1 | Specific issue | High | L1/L2/L3 | File, line, or observed behavior | Why it matters |

List only findings that materially affect activation, semantic clarity, logical self-consistency, task success, safety, maintainability, or eval trustworthiness.

## Recommended Fixes

| Priority | Fix | Impact | Effort |
| ---: | --- | --- | --- |
| 1 | Concrete change an implementer can make | High | Low/Medium/High |

Prefer exact changes: frontmatter rewrite, term definition, precedence rule, missing workflow section, linked reference split, script validation, safer command behavior, or better eval expectations.

## Verification Plan

Include checks that prove the fixes worked:

- Static validation or script smoke tests
- Trigger prompts that should and should not activate the skill
- Realistic task prompts with expected outcomes
- Baseline comparison against no skill or the previous skill version for important skills
- Manual review steps for subjective outputs
- Safety checks for credentials, destructive operations, and file writes

## Limits

State missing artifacts, assumptions, uncertainty, and any dimensions marked `N/A`. Mention the smallest additional evidence that would improve confidence.

## Optional Saved Report

When the user asks to write a report file, save this structure as `EVAL.md` in the evaluated skill directory unless they name another path.
