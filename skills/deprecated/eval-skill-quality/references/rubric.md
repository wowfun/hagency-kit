# Skill Quality Rubric

Use this rubric to score skills on a 100-point quality scale. Score only what the evidence supports. Mark a dimension `N/A` when artifacts are missing and exclude it from the denominator.

## Scoring

| Score Band | Meaning | Readiness |
| --- | --- | --- |
| 90-100 | Excellent | Publish confidently |
| 80-89 | Good | Publishable with known minor gaps |
| 70-79 | Acceptable | Fix high-impact gaps before broad use |
| 60-69 | Needs work | Fix blockers and weak dimensions first |
| Below 60 | Not ready | Redesign or substantially revise |

When assigning a partial dimension score, use this anchor:

| Percent of Dimension | Meaning |
| ---: | --- |
| 0-24% | Missing, broken, or unsafe |
| 25-49% | Present but inadequate |
| 50-69% | Functional with notable gaps |
| 70-89% | Solid with minor issues |
| 90-100% | Strong, evidence-backed, low risk |

## Evidence Requirements

Use evidence levels for any low-scoring dimension, high-risk finding, SRL conclusion, or publish-readiness recommendation:

| Level | Meaning | Weight |
| --- | --- | --- |
| L1 | The skill states a rule, claim, workflow step, or safety behavior. | Useful, but not proof of behavior |
| L2 | The workflow, script, template, or bundled asset implements the behavior. | Stronger evidence |
| L3 | Tests, eval runs, transcripts, or observed execution prove the behavior works. | Strongest evidence |

For minor observations, concise file or behavior evidence is enough. For publishing conclusions, explicitly separate documented, implemented, and verified capabilities.

## SRL Overlay

Use [srl-framework.md](srl-framework.md) as a reliability lens when the user asks about production use, automation, factual correctness, external data, reproducibility, or publish-readiness.

- SRL does not replace the 100-point quality score and does not cap it automatically.
- Low SRL should reduce publish-readiness, require manual review, or constrain usage even when the quality score is high.
- A high-quality creative or advisory skill may naturally have lower SRL; explain the use constraint rather than penalizing unrelated dimensions.
- A skill that produces factual, operational, financial, legal, security, or automation-relevant outputs needs stronger SRL evidence.

## 1. Activation Reliability - 15 points

Can the agent find and activate the skill when it is useful, without over-triggering?

Evidence to inspect:
- Frontmatter `name` and `description`
- Trigger words, user intents, excluded contexts, semantic overlap, and neighboring skill overlap
- Any trigger evals or examples of relevant and irrelevant prompts
- Whether the skill clearly disambiguates itself from neighboring skills or overlapping workflows

High score indicators:
- Description includes the task domain, action verbs, and concrete "use when" contexts.
- Trigger wording matches user intent rather than implementation details only.
- Scope is distinct from nearby skills, with low false-positive and false-negative risk.

Common issues:
- Description is too short, vague, or only says what the skill is.
- Trigger words are too broad and compete with unrelated skills.
- Trigger contexts overlap another skill without an ownership or precedence rule.
- Trigger wording implies a broader domain than the instructions can actually handle.
- Important user phrases are absent, so the skill under-triggers.

Quick wins:
- Rewrite the description around user intent: "Use when asked to..."
- Add missing domain terms, artifact names, and review/action verbs.
- Narrow broad claims that could steal unrelated requests.

## 2. Task Coverage and Functional Suitability - 20 points

Does the skill cover the tasks it claims to support, using an appropriate level of detail?

Evidence to inspect:
- Claimed capabilities in the description and body
- Workflow steps, decision criteria, examples, scripts, and limitations
- Whether claims, examples, scripts, and evals describe the same behavior
- Existing outputs or transcripts showing the skill in use
- Whether external facts, commands, APIs, or files anchor the workflow when factual reliability matters

High score indicators:
- Core workflows are complete enough for a fresh agent to execute.
- Decision points include criteria, not vague phrases like "when appropriate."
- Known limitations and prerequisites are stated where they affect success.
- The claimed scope matches the actual workflow, examples, and bundled resources.
- Tools, scripts, or references fit the problem without unnecessary complexity.

Common issues:
- Broad claims with only happy-path instructions.
- Missing output contracts, input assumptions, or edge-case handling.
- Description promises behavior that the body, scripts, or examples do not support.
- Eval tasks or examples imply unsupported behavior or hidden requirements.
- Heavy scripts or dependencies where a simple workflow would suffice.

Quick wins:
- Add the smallest missing workflow step or output contract.
- State prerequisites and unsupported cases.
- Move niche or advanced instructions into a linked reference.

## 3. Instruction Clarity, Semantic Consistency, and Agent Usability - 20 points

Can an agent follow the skill correctly on the first try?

Evidence to inspect:
- Structure and readability of `SKILL.md`
- Definitions, scope boundaries, preconditions, ordering of instructions, examples, output formats, and anti-patterns
- Consistency across commands, flags, terminology, and report formats
- Internal consistency across the description, body, references, examples, scripts, and evals
- Whether mandatory rules, defaults, exceptions, and fallback paths can be applied without contradiction

High score indicators:
- The role, inputs, workflow, rules, and output contract are explicit.
- Key terms and scope boundaries have one stable meaning.
- Instructions are ordered by execution flow and importance.
- Defaults, exceptions, and precedence rules are clear when instructions could collide.
- The same input class maps to the same action across description, workflow, examples, references, and scripts.
- Examples are concise and representative, not answer leaks.
- Error handling and uncertainty handling are clear.

Common issues:
- Contradictory rules or unclear priority between rules.
- Semantic ambiguity in key terms, task ownership, trigger boundaries, or expected outputs.
- Rules that can both apply to the same scenario without an explicit precedence rule.
- Different files define incompatible output shapes, input assumptions, or safety behavior.
- Examples, scripts, or templates imply behavior not described in the workflow.
- Undefined output shape, so agents improvise reports or artifacts.
- Excessive explanation that hides the actual procedure.

Quick wins:
- Convert prose into a short ordered workflow.
- Add a report contract or output template.
- Replace vague conditions with concrete criteria.
- Define overloaded terms and add precedence rules for exceptions.
- Align examples, scripts, and evals with the same stated behavior.

## 4. Context Efficiency and Progressive Disclosure - 15 points

Does the skill preserve context while still exposing necessary knowledge?

Evidence to inspect:
- `SKILL.md` line count and density
- Whether detailed material lives in linked `references/`
- Whether references are loaded conditionally and are easy to find

High score indicators:
- `SKILL.md` stays focused on essential workflow and routing.
- Detailed rubrics, schemas, examples, or provider-specific guidance live in references.
- Every reference is linked from `SKILL.md` with guidance on when to read it.
- The agent can use the skill without reading bundled source code for basic tasks.

Common issues:
- Long background sections that do not affect execution.
- Everything is in one large `SKILL.md` despite multiple variants or domains.
- Reference files exist but are not linked or described.

Quick wins:
- Move long criteria, examples, or schemas into `references/`.
- Add one sentence telling the agent when to read each reference.
- Remove generic advice the base model already knows.

## 5. Safety, Reliability, and Maintainability - 15 points

Is the skill safe to use, resilient to normal errors, and easy to maintain?

Evidence to inspect:
- Scripts, shell commands, dependencies, environment variables, and write behavior
- Input validation, dry-run behavior, idempotency, recovery paths, and error messages
- File organization, modularity, and testability
- Credential and sensitive data handling
- SRL evidence for failure transparency, reproducibility, traceability, and external fact anchoring

High score indicators:
- Destructive actions require explicit user intent and have recovery guidance.
- Scripts validate inputs, report actionable errors, and avoid hardcoded secrets.
- External dependencies are documented and justified.
- Re-running commands is safe or the risk is clearly called out.
- Low-confidence, stale, conflicting, or missing data leads to stopping, explicit degradation, or manual review.
- Code is small, readable, and testable.

Common issues:
- Unsafe defaults, silent writes, or unclear side effects.
- Undocumented environment variables or credentials.
- Monolithic scripts with raw tracebacks for common failures.
- No guidance for interrupted or failed operations.
- Factual outputs rely on model judgment without traceable sources or a failure path.

Quick wins:
- Add validation, `--dry-run`, `--json`, or clear stderr messages where useful.
- Document required environment variables.
- Split fragile script logic into small functions.
- State when the agent must ask before mutating files.

## 6. Eval Quality, Leakage Resistance, and Real-World Value - 15 points

Does the skill have evidence that it improves outcomes, and are evaluations trustworthy?

Evidence to inspect:
- Eval prompts, expectations, transcripts, benchmark summaries, and user feedback
- Baseline comparisons with and without the skill when available
- Whether examples or references contain answers to eval tasks

High score indicators:
- Evals cover realistic prompts and failure modes.
- Expectations are discriminating: they pass only when the task is genuinely done.
- Baselines or prior versions show the skill improves quality, speed, cost, or consistency.
- Leakage is controlled: eval tasks are not near-copies of examples or embedded answers.

Common issues:
- Eval prompts mirror examples too closely.
- Assertions check surface compliance, such as file existence, without checking substance.
- No comparison to no-skill or previous-skill behavior.
- Claimed value is not backed by transcripts, tests, or user feedback.

Quick wins:
- Add 3-5 realistic prompts with clear expected outcomes.
- Include at least one negative or edge-case prompt.
- Rewrite weak assertions to verify content, process, and output quality.
- Compare against a baseline for high-impact or publish-bound skills.

## Finding Severity

Use severity to prioritize fixes:

| Severity | Meaning |
| --- | --- |
| Critical | Likely unsafe behavior, data exposure, destructive action, or complete skill failure |
| High | Likely missed triggers, semantic conflicts, wrong outputs, hidden failures, or misleading evals |
| Medium | Meaningful reliability, usability, or maintainability risk with workarounds |
| Low | Polish, documentation gaps, or minor clarity improvements |

For each important finding, cite evidence from a file, line, output, or observed behavior when possible.

## Publish-Readiness Guidance

- **Publish confidently** only when high-risk findings are absent, quality score is strong, and SRL constraints match the intended use.
- **Publish with constraints** when the quality score is good but SRL is low for factual or operational decisions; require human review or limit usage to advisory contexts.
- **Do not publish broadly** when critical ambiguity, logical contradiction, unsafe side effects, leakage-prone evals, or low-confidence factual workflows remain unresolved.
