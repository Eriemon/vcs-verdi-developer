# Scripts Evolution Template

- Template family: skill-template
- Category path: agent-governance
- Target type: vcs-verdi-developer
- Source file: 2-scripts.md
- Version window: current-plus-latest-history
- Target source: inferred
- Rationale: Inferred from project control profile, repository facts, and topic keywords.

## Source Versions
- `docs/experience/2-scripts.md`
- `docs/experience/history_experience/20260515-231403/2-scripts.md`

## Evidence Sources
run_quality_gate.py, remote_eda_gate.py, run_remote_eda_smoke.sh, collect_evidence.py, test_skill_structure.py, and unit test output from the v0.5.1 closure.

## Applicable Scenario
Use this script pattern when a skill needs deterministic wrappers around fragile external tools and must distinguish dry-run plans from real execution evidence.

## Distilled Workflow
Make each script own one validation boundary: command planning, bundle creation, execution, evidence collection, or final confidence classification. Emit JSON for every gate. Keep missing tools as explicit blockers. Add tests that assert both required command fragments and forbidden host-specific fragments. Rebuild release artifacts after every script change.

## Key Decisions
Prefer generic remote EDA naming. Avoid hardcoded host paths. Keep local_confidence and eda_execution_confidence as separate state transitions.

## Common Problems
A human-readable matrix term can drift from a script audit token. A passing dry-run can be mistaken for a passing simulator. Shell wrappers can hide host-specific assumptions.

## Non-Reusable Content
Do not reuse specific server names, Windows workspace paths, or proprietary installation paths.

## Application Checklist
Name the CLI, define JSON status fields, add red tests, run unit tests, run quality gate, and check the packaged script copy.
