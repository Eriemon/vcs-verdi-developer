# Workflow Evolution Template

- Template family: skill-template
- Category path: agent-governance
- Target type: vcs-verdi-developer
- Source file: 1-workflow.md
- Version window: current-plus-latest-history
- Target source: inferred
- Rationale: Inferred from project control profile, repository facts, and topic keywords.

## Source Versions
- `docs/experience/1-workflow.md`
- `docs/experience/history_experience/20260515-231403/1-workflow.md`

## Evidence Sources
Current v0.5.1 handoff, development notes, changelog, local quality gate output, release package receipt, and skill payload scan tests.

## Applicable Scenario
Use this template when a skill repository must turn a broad user confidence request into a bounded, factual release claim while preserving AGENTS and docs governance.

## Distilled Workflow
Inspect repository facts and the control profile first. Align AGENTS.md, docs governance, scripts, tests, release, and install decisions before synthesis. Implement by changing SKILL resources and deterministic scripts, verify with unit tests and quality gates, package through release tooling, then install deliberately. Keep a feedback loop that downgrades unsupported execution evidence to blocked instead of weakening the claim.

## Key Decisions
Separate local confidence from proprietary EDA execution confidence. Keep temporary server names out of the skill. Treat release zip parity and local installation as explicit acceptance criteria.

## Common Problems
Generated docs can be structurally valid but semantically empty if JSON payloads are malformed. Release packages can become stale after a script fix. Real tool evidence can lag local planning support.

## Non-Reusable Content
Do not copy project-specific VCS, Verdi, URG, or host evidence into a generic template.

## Application Checklist
Check control profile, write tests, patch scripts and references, run validation, package on master, install locally, and report blocked evidence honestly.
