<p align="center">
  <a href="README.md"><strong>English</strong></a>
  <span>&nbsp;|&nbsp;</span>
  <a href="README-CN.md">中文</a>
</p>

<p align="center">
  <img src="docs/assets/hero.svg" alt="VCS Verdi Developer" width="100%">
</p>

<p align="center">
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/badge/license-Apache--2.0-1f6feb"></a>
  <a href="pyproject.toml"><img alt="Python" src="https://img.shields.io/badge/python-3.10%2B-2f81f7"></a>
  <img alt="Version" src="https://img.shields.io/badge/version-v0.1.0-7c3aed">
  <a href="SKILL.md"><img alt="Agent Skill" src="https://img.shields.io/badge/agent-skill-16a34a"></a>
  <a href="references/vcs-verdi-flow.md"><img alt="Target" src="https://img.shields.io/badge/target-VCS%20%26%20Verdi-f59e0b"></a>
</p>

<h1 align="center">VCS Verdi Developer</h1>

<p align="center">
  A Codex-ready agent skill for Synopsys VCS simulation and Verdi waveform-debug workflows.
</p>

VCS Verdi Developer helps an AI coding agent work more reliably with Synopsys VCS and Verdi. It packages workflow instructions, reusable fixtures, reference material, evaluation cases, and deterministic helper scripts for compile, simulate, FSDB generation, RC layout generation, Verdi interaction, and remote validation flows.

This repository is primarily an **agent skill package**. The main interface is the skill surface an agent can load and follow, while the scripts provide deterministic checks and helper behavior around the proprietary tool flow.

## Why It Exists

EDA workflows are easy to mis-handle when an agent guesses environment readiness, GUI assumptions, or dump/debug steps. VCS Verdi Developer adds a disciplined layer for:

- environment readiness checks before proprietary tools run,
- repeatable VCS compile/elaborate/simulate command planning,
- FSDB and Verdi load verification,
- generated signal-restore RC layouts,
- ThreadFPGA-oriented remote validation gates.

## Skill Architecture

<p align="center">
  <img src="docs/assets/architecture.svg" alt="VCS Verdi Developer skill architecture" width="100%">
</p>

## Workflow

<p align="center">
  <img src="docs/assets/workflow.svg" alt="VCS Verdi Developer workflow" width="100%">
</p>

## Repository Map

| Path | Purpose |
| --- | --- |
| `SKILL.md` | Agent-facing routing, workflow, constraints, and safety boundaries. |
| `agents/openai.yaml` | UI metadata for invoking the skill. |
| `scripts/` | Deterministic helpers for environment checks, RC generation, smoke planning, and remote wrappers. |
| `references/` | VCS/Verdi flow rules, RC format guidance, interaction notes, and validation gates. |
| `assets/` | Minimal fixtures and waveform-layout source templates. |
| `evals/` | Skill evaluation cases that define expected with-skill behavior. |
| `RELEASE_RECEIPT.json` | Provenance record for the imported `v0.1.0` release package. |

## Quick Start

Place this repository in a Codex skill search path to use it as an agent skill. For local inspection and helper usage:

```powershell
python .\scripts\check_env.py --json
python .\scripts\generate_rc.py --help
python .\scripts\smoke_vcs_verdi.py --help
```

Typical workflow:

1. Probe readiness with `scripts/check_env.py --json`.
2. Generate or review waveform layout RC files with `scripts/generate_rc.py`.
3. Plan a minimal VCS/Verdi smoke flow with `scripts/smoke_vcs_verdi.py --dry-run`.
4. Execute only after the exact command plan and environment findings are reviewed.

## Scope

VCS Verdi Developer is intentionally narrow:

- It focuses on Synopsys VCS and Verdi workflows, not general RTL development or synthesis.
- It does not claim proprietary-tool validation passed unless the exact VCS/Verdi flow actually ran.
- It treats GUI-driven Verdi usage as optional and environment-dependent.
- It should not expose real license values, internal server details, private paths, or private infrastructure data.

## Security and Sensitive Data

- Do not commit real license files, secret tokens, private keys, or internal host details.
- Do not treat environment hints like `SNPSLMD_LICENSE_FILE` as harmless metadata if they contain private values.
- Review imported release artifacts for sensitive content before publishing or pushing.

## Citation

If this skill helps your workflow, please cite it using the metadata in [CITATION.cff](CITATION.cff).

```bibtex
@software{eriemon_2026_vcs_verdi_developer,
  author       = {Eriemon},
  title        = {{VCS Verdi Developer}: An Agent Skill for Synopsys VCS and Verdi Workflows},
  year         = {2026},
  version      = {0.1.0},
  date         = {2026-05-15},
  url          = {https://github.com/Eriemon/vcs-verdi-developer},
  license      = {Apache-2.0},
  note         = {Agent skill package for Synopsys VCS simulation and Verdi waveform-debug workflows}
}
```

## License

Apache License 2.0. See [LICENSE](LICENSE).
