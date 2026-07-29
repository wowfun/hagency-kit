---
name: eval-skill-quality
description: Evaluate skills for quality, activation reliability, publish-readiness, semantic clarity, logical self-consistency, SRL reliability, leakage risk, context efficiency, safety, maintainability, and real-world value. Use when asked to review, audit, score, benchmark, improve, compare, or prepare a skill before publishing, especially when inspecting skill instructions, bundled scripts, references, eval prompts, or observed behavior.
---

# Eval Skill Quality

## Role

Act as an evidence-based skill quality evaluator. Review the skill as an agent-facing or workflow product: it should trigger at the right time, define its concepts clearly, stay logically self-consistent, teach the agent enough to succeed, avoid wasting context, stay safe to use, and measurably improve outcomes over not using the skill. Use SRL as a reliability lens alongside the 100-point quality assessment, not as a replacement for it.

Match the user's language by default. Report results directly in chat unless the user explicitly asks for a saved report, revised skill, or generated eval assets.

## Inputs

Use the evidence available in the current task:

- `SKILL.md` frontmatter, description, body, and linked references
- Bundled `scripts/`, `references/`, `assets/`, examples, templates, and eval prompts
- Existing evaluation results, transcripts, test outputs, or user feedback
- Key terms, scope boundaries, decision rules, exceptions, and precedence statements
- Neighboring skills or internal sections that may conflict with trigger behavior or task ownership
- Repository conventions for skill layout and validation

The static checker is `SKILL.md` directory-first. Manual review can still evaluate broader skill-like artifacts, but clearly state when the target does not follow the standard skill directory layout.

## Information Sufficiency

Classify the assessment before scoring:

- **Insufficient information**: refuse to assign scores when `SKILL.md` or equivalent instructions are missing, the effective instructions are under 10 non-empty lines, or no recognizable workflow, output contract, or decision rule exists.
- **Low confidence**: continue with low-confidence scoring when evidence is thin, such as short instructions, no examples, no scripts, no evals, no transcripts, or a self-review scenario.
- **Normal confidence**: score normally when the instructions and supporting artifacts are sufficient to assess claims, behavior, reliability, and risks.

Ask for more information only when the missing artifact would make the requested conclusion misleading.

## Workflow

1. Identify the skill's claimed purpose, target user intent, trigger contexts, core concepts, expected outputs, and bundled resources.
2. Run the optional static checker when a local skill path is available and command execution is appropriate:

   ```sh
   python3 scripts/eval_skill_quality.py /path/to/skill
   ```

   Use `--json` when another tool or report needs machine-readable results.

3. Read [references/rubric.md](references/rubric.md) before scoring. Read [references/srl-framework.md](references/srl-framework.md) when judging reliability, traceability, hallucination exposure, failure transparency, reproducibility, or publish-readiness.
4. Score only dimensions supported by evidence; mark unsupported dimensions `N/A` rather than guessing.
5. Look for evidence of actual skill value, not just tidy structure. A polished skill can still fail if it under-triggers, over-claims, contains ambiguous or conflicting instructions, leaks answers into evals, has low SRL reliability, or does not improve outcomes.
6. Prioritize findings by user impact, likelihood, controllability, and effort. Separate blockers from polish.
7. Use [references/report-template.md](references/report-template.md) for the final report shape unless the user requests a different format.

## Evidence Levels

Use evidence-first reasoning for high-risk findings, low-scoring dimensions, SRL conclusions, and publish-readiness recommendations:

| Level | Meaning |
| --- | --- |
| L1 | The skill states a rule, claim, workflow step, or safety behavior. |
| L2 | The workflow, script, template, or bundled asset implements the claimed behavior. |
| L3 | Tests, eval runs, transcripts, or observed execution prove the behavior works. |

Do not treat L1 as proof that a behavior works. For publishing conclusions, distinguish documented, implemented, and verified capabilities.

## Evaluation Rules

- Treat the skill description as the primary activation interface. It should say when to use the skill, not just what the skill is.
- Flag semantic ambiguity when key terms, scope, preconditions, decision criteria, or output expectations can be read in multiple materially different ways.
- Check logical self-consistency across the description, body, examples, references, scripts, and evals. Rules, defaults, exceptions, and templates should not contradict each other; if they can conflict, the skill should state precedence.
- Treat conflict risk broadly: internal contradictions, overlap with neighboring skills, mismatch between claimed and actual behavior, and examples that imply behavior the workflow does not support.
- Use SRL to assess whether outputs are anchored in externally verifiable facts. Low SRL should lower publish-readiness or require manual review, but it should not directly cap the quality score.
- Reward progressive disclosure: keep core workflow in `SKILL.md`, and move detailed examples or long criteria into clearly linked references.
- Penalize unsupported claims. If the skill says it handles a workflow, it should include enough instructions, examples, scripts, or evals for an agent to do that workflow reliably.
- Check leakage when evals or examples exist. Test prompts should require genuine application of the skill, not pattern matching against answers embedded in the skill.
- Prefer concrete fixes over broad advice. Recommend exact description changes, missing reference sections, safer script behavior, better eval expectations, or simpler scope boundaries.
- Do not require every skill to have scripts or eval harnesses. Match expectations to the skill's purpose, risk, and complexity.

## Report Contract

Include these sections by default:

1. **Summary**: overall score or readiness, highest-risk issues, and confidence level.
2. **Scorecard**: quality dimension scores, SRL level, and short evidence-backed rationale.
3. **Reliability / SRL**: reliability risks, source anchoring, reproducibility, and use constraints.
4. **Critical Findings**: prioritized blockers, ambiguity or conflict risks, and important quality issues with L1/L2/L3 evidence levels where relevant.
5. **Recommended Fixes**: concrete changes with expected impact and effort.
6. **Verification Plan**: checks, eval prompts, smoke tests, or manual review steps.
7. **Limits**: missing evidence, assumptions, confidence level, and areas not assessed.

If the user asks for a short review, compress the report but keep the score, top findings, and next fixes.

## Optional Saved Report

Only when requested, save the report as `EVAL.md` in the evaluated skill directory or another path chosen by the user. Use the report template rather than inventing a new schema.
