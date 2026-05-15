# Design UI Evolution Template

- Template family: skill-template
- Category path: agent-governance
- Target type: vcs-verdi-developer
- Source file: 4-design-ui.md
- Version window: current-plus-latest-history
- Target source: inferred
- Rationale: Inferred from project control profile, repository facts, and topic keywords.

## Source Versions
- `docs/experience/4-design-ui.md`
- `docs/experience/history_experience/20260515-231403/4-design-ui.md`

## Evidence Sources
SKILL.md wording changes, reference file names, metadata descriptions, and the v0.5.0 payload scan forbidding temporary server branding.

## Applicable Scenario
Use this content-design pattern when a skill must be reusable across environments and should not expose temporary validation infrastructure as part of its product identity.

## Distilled Workflow
Design the wording around roles and capabilities rather than a particular machine. Put concise triggers and safety boundaries in SKILL.md. Move detailed procedures into references. Use scripts for deterministic behavior. Validate the content design with automated scans that reject old branding or host-specific paths.

## Key Decisions
Remote EDA host is a reusable concept; the temporary validation server name is not. GUI behavior remains guarded and outside the non-GUI confidence promise.

## Common Problems
Skill descriptions can accidentally overclaim official tool coverage. Reference names can brand a private validation host. Tests can accidentally forbid the new generic wording if refactored carelessly.

## Non-Reusable Content
Do not copy project-specific Synopsys evidence, host labels, or old release wording into general UI or skill descriptions.

## Application Checklist
Review trigger text, resource map, reference names, script names, metadata, and payload scans before release.
