# SRL Framework

SRL (Skill Reliability Level) is a reliability lens for skill evaluation. It asks how much of a skill's output is anchored in externally verifiable facts rather than unverified model reasoning.

Use SRL alongside the quality rubric. SRL affects publish-readiness and required human review, but it does not replace or automatically cap the quality score.

## SRL Levels

| Level | Reliability Profile | Usage Recommendation |
| --- | --- | --- |
| SRL-5 | Full workflow is anchored in strong-contract tools such as CLI, official APIs, databases, local files, or deterministic scripts. AI mainly orchestrates and formats. | Suitable for automation when safety controls are also adequate. |
| SRL-4 | Key steps are anchored in reliable external facts; limited AI reasoning; failures are visible. | Publishable for bounded workflows, with normal review. |
| SRL-3 | Mixed reliable and weak sources; some long-context reasoning or weak validation remains. | Use within clear boundaries; require review for important decisions. |
| SRL-2 | Few external facts; many steps depend on open web data, user-generated content, or AI inference. | Advisory only; human verification required. |
| SRL-1 | Almost entirely AI reasoning with no meaningful external verification. | Do not use for formal decisions or automation. |

## Evidence Levels

Use these levels when making SRL conclusions, high-risk findings, and publish-readiness calls:

| Level | Meaning | Example |
| --- | --- | --- |
| L1 | The skill states a rule or behavior. | `SKILL.md` says to validate API results. |
| L2 | The workflow, script, template, or asset implements the behavior. | A script checks HTTP status and exits on failure. |
| L3 | Tests, eval runs, transcripts, or observed execution prove the behavior works. | A failing API response was tested and produced the expected stop condition. |

L1 is useful evidence, but it is not proof that behavior works. Prefer L2 or L3 for publish-readiness conclusions.

## Dimensions

### Anchor Density

How many workflow steps rely on externally verifiable engineering anchors?

Anchors include:
- CLI or shell commands with inspectable output
- Official APIs, databases, local files, or deterministic scripts
- MCP/tool calls with structured inputs and outputs
- Write-then-read or call-then-validate loops

Anchoring questions:
- Which steps have CLI/API/DB/file/tool anchors?
- Which steps have no external anchor?
- Does the workflow verify results after performing an action?

### Hallucination Exposure

How much of the workflow depends on unverified AI reasoning?

Anchoring questions:
- Which steps are pure AI inference?
- Which AI reasoning steps are later checked against external evidence?
- Is the final output structured and verifiable, or mainly free text?

### Failure Transparency

Does the skill stop, degrade, or disclose uncertainty when reliable information is missing?

Anchoring questions:
- What happens when inputs are missing, malformed, stale, or conflicting?
- Does the skill define stop conditions or low-confidence paths?
- Does it tell the agent when to say "unknown", "not enough evidence", or "manual review required"?

### Traceability

Can users trace outputs back to specific, verifiable sources?

Source strength:
- Strong: official API, CLI output, database query, local artifact
- Medium: third-party API or curated aggregate source
- Weak: webpage scraping, search results, volatile public content
- None: model knowledge or unsupported inference

Anchoring questions:
- What sources does the skill rely on?
- Does the report cite source paths, commands, timestamps, URLs, or records?
- Can a user independently verify the output?

### Reproducibility

Would the same input likely produce the same output later?

Anchoring questions:
- Are external systems versioned, cached, or stable?
- Does the skill persist intermediate state or checkpoints?
- Are there random, time-sensitive, search-dependent, or model-dependent steps?

## Correction Lenses

Use these as qualitative modifiers to the SRL conclusion:

| Lens | Question | Risk Signal |
| --- | --- | --- |
| Timeliness | Will the data or interface still be valid in six months? | Unversioned APIs, changing webpages, stale cached data |
| Robustness | Does the skill handle noisy, empty, contradictory, or malformed inputs? | Assumes perfect input or single-source truth |
| Cascade Stability | Do errors compound across dependent steps? | Long chains where each step consumes the prior step as the only input |

## Reporting Guidance

- Report a single SRL level plus a short rationale.
- Do not over-precision SRL with fake decimal scores unless the user explicitly requests a deeper reliability audit.
- If a skill is creative or advisory, low SRL may be acceptable; state the use boundary.
- If a skill informs factual, operational, financial, legal, security, or automated decisions, low SRL should require manual review or block broad publishing.
