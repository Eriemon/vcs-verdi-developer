# Contributing

Thank you for improving VCS Verdi Developer. This repository is an agent skill first: changes should help an AI coding agent perform Synopsys VCS and Verdi work more reliably, not only add standalone script behavior.

## Contribution Principles

- Keep `SKILL.md` concise, actionable, and scoped to agent behavior.
- Move detailed workflow notes, capability boundaries, command nuances, and format rules into `references/`.
- Keep deterministic helper logic in `scripts/`.
- Keep minimal reusable fixtures, manifests, and include files in `assets/`, and behavior expectations in `evals/`.
- Do not claim VCS, Verdi, or default remote EDA server validation passed unless the exact flow actually ran and left evidence.
- Keep generated outputs, wave dumps, coverage reports, local logs, machine-specific paths, license values, and private infrastructure details out of commits.

## Suggested Workflow

1. Identify whether the change affects environment discovery, RC generation, smoke planning, project import, coverage/FSDB tooling, regression flow, remote validation, or documentation.
2. Make a focused change with a clear before/after behavior.
3. Run the smallest relevant validation or static check.
4. Include the exact evidence for any claimed VCS/Verdi behavior change.

## Validation

Useful local checks:

```powershell
python .\scripts\check_env.py --json
python .\scripts\generate_rc.py --help
python .\scripts\smoke_vcs_verdi.py --help
python .\scripts\import_vcs_project.py --help
python .\scripts\coverage_flow.py --help
python .\scripts\run_regression.py --help
```

Use dry-run planning by default for VCS/Verdi smoke flows. Do not claim proprietary-tool acceptance from static inspection alone. For coverage, FSDB utilities, regression, or remote evidence flows, keep execution claims separated from local planning claims.

## Documentation Expectations

- Keep the default `README.md` in English.
- Put Chinese user-facing documentation in `README-CN.md`.
- Keep examples short and reproducible.
- Do not publish private server names, real license values, or internal filesystem paths in docs.
