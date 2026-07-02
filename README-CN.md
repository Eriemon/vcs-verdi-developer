<p align="center">
  <a href="README.md">English</a>
  <span>&nbsp;|&nbsp;</span>
  <a href="README-CN.md"><strong>中文</strong></a>
</p>

<p align="center">
  <img src="docs/assets/hero.svg" alt="VCS Verdi Developer" width="100%">
</p>

<p align="center">
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/badge/license-Apache--2.0-1f6feb"></a>
  <a href="pyproject.toml"><img alt="Python" src="https://img.shields.io/badge/python-3.10%2B-2f81f7"></a>
  <img alt="Version" src="https://img.shields.io/badge/version-v0.5.4-7c3aed">
  <a href="SKILL.md"><img alt="Agent Skill" src="https://img.shields.io/badge/agent-skill-16a34a"></a>
  <a href="references/vcs-verdi-flow.md"><img alt="Target" src="https://img.shields.io/badge/target-VCS%20%26%20Verdi-f59e0b"></a>
</p>

<h1 align="center">VCS Verdi Developer</h1>

<p align="center">
  面向 Synopsys VCS 仿真与 Verdi 波形调试流程的 Codex agent skill。
</p>

VCS Verdi Developer 用来帮助 AI 编程代理更可靠地处理 Synopsys VCS 与 Verdi 工作流。仓库中包含工作流说明、可复用夹具、参考文档、评测样例，以及面向非 GUI 编译/仿真规划、FSDB 生成与读回、RC 布局、项目导入、cocotb VCS/VPI 规划、coverage 诊断、回归执行、claim/evidence 门禁、Verdi 交互和远程验证的确定性辅助脚本。

这个仓库首先是一个 **agent skill package**。主要入口是代理可加载、可遵循的 skill 结构，而脚本部分负责围绕专有 EDA 工具流程提供可重复的检查与辅助能力。

## 为什么需要它

EDA 工作流很容易因为环境判断、GUI 假设或 dump/debug 步骤处理不当而出错。VCS Verdi Developer 主要补上这些工程化环节：

- 在调用专有工具前先做环境就绪检查；
- 用可重复的方式规划 VCS compile / elaborate / simulate；
- 支持 manifest 驱动的项目导入与 non-GUI 流程归一化；
- 支持 cocotb VCS/VPI 规划，并保持受控边界；
- 支持 FSDB 生成、读回与转换规划；
- 支持 coverage 与 URG 诊断规划；
- 支持回归批量执行与证据收集；
- 在 readiness / factual claim 前增加 claim/evidence 门禁；
- 校验 Verdi 加载；
- 生成可复用的 signal-restore RC 布局；
- 提供面向默认远程 EDA 服务器的验证门禁。

## Skill 架构

<p align="center">
  <img src="docs/assets/architecture-cn.svg" alt="VCS Verdi Developer Skill 架构" width="100%">
</p>

## 工作流

<p align="center">
  <img src="docs/assets/workflow-cn.svg" alt="VCS Verdi Developer 工作流" width="100%">
</p>

## v0.5.4 重点更新

- 把脚本入口重组为 `scripts/python/`、`scripts/powershell/`、`scripts/shell/` 和 `scripts/bat/` 四层布局，其中 Python 树作为规范实现，其余平台 wrapper 与之保持一致。
- 把 coverage、evidence、remote EDA gate 和 smoke validation 拆成独立脚本组，公开仓库里能直接看清不同能力面的入口和边界。
- 退役旧的 `assets/templates/evolution/` 公共载荷，把被跟踪的本地验证资产移出公开仓库，只保留适合公开发布的 skill 运行时、参考资料和文档。
- 增加了基于仓库状态重建 GitHub release 资产的路径，发布时不会直接上传 `tmp/` 下的原始 staging 压缩包。

## 仓库结构

| 路径 | 作用 |
| --- | --- |
| `SKILL.md` | 面向 agent 的路由、流程、约束和安全边界。 |
| `agents/openai.yaml` | skill 调用入口的 UI 元数据。 |
| `scripts/` | 规范 Python 实现，加上 PowerShell、shell 和 bat wrapper；覆盖环境检查、smoke 校验、项目导入、coverage、evidence、回归和远程 EDA 辅助流程。 |
| `references/` | VCS/Verdi 流程规则、能力边界、non-GUI 指南、RC 格式说明和验证门禁。 |
| `assets/` | 发布版 skill 使用的最小夹具、evidence 样例、include 文件、manifest 矩阵和波形模板。 |
| `evals/` | 定义 with-skill 预期行为的评测样例。 |
| `docs/assets/` | 仓库 README 页面使用的展示图。 |
| `build_release.py` | 根据审查后的仓库状态重建公开 GitHub release zip，并排除本地验证与私有路径。 |
| `RELEASE_RECEIPT.json` | 导入的 `v0.5.4` staging 发布包来源记录；GitHub release 资产会基于当前仓库状态重新构建后再上传。 |

如果需要固定公开版本，请使用 `v0.5.4` tag 或 GitHub Releases 中重建得到的 `erie-vcs-verdi-developer-v0.5.4.zip` 资产。

发布来源说明：`v0.5.4` 的 GitHub release 资产是在导入、审查并清理公开边界后，基于当前仓库状态重新构建的。`tmp/` 下的原始压缩包只作为本地导入输入，不会直接上传。

## 快速开始

把本仓库放入 Codex skill 搜索路径即可作为 agent skill 使用。做本地检查或调用辅助脚本时：

```powershell
python .\scripts\python\env\check_env.py --json
python .\scripts\python\validation\vcs_verdi_check.py --help
python .\scripts\python\quality\run_quality_gate.py --json
python .\scripts\python\import\import_vcs_project.py --help
python .\scripts\python\flows\cocotb_vcs_flow.py --help
python .\scripts\python\coverage\coverage_flow.py --help
python .\scripts\python\evidence\evidence_claim_gate.py --help
python .\scripts\python\remote\remote_eda_gate.py --help
```

如果需要贴合具体平台的原生 shell，也可以改用 `scripts/powershell/`、`scripts/shell/` 和 `scripts/bat/` 下与 Python 入口一一对应的 wrapper。

推荐顺序：

1. 先用 `scripts/python/env/check_env.py --json` 探测环境。
2. 如果输入来自 Makefile、filelist、Edalize/CAPI2 或类似项目元数据，先用 `scripts/python/import/import_vcs_project.py` 做项目导入或归一化。
3. 用 `scripts/python/validation/vcs_verdi_check.py --dry-run` 规划最小或 manifest 驱动的 VCS/Verdi smoke 流程。
4. 当任务扩展到 coverage、证据收集、远程主机门禁或发布复核时，使用 `scripts/python/coverage/coverage_flow.py`、`scripts/python/evidence/collect_evidence.py`、`scripts/python/evidence/evidence_claim_gate.py`、`scripts/python/remote/remote_eda_gate.py` 和 `scripts/python/quality/run_quality_gate.py`。
5. 只有在命令计划和环境结论都被确认后，才执行真实流程；如果只是换调用壳层，优先复用对应的 PowerShell、shell 或 bat wrapper，而不是改动 Python 规范实现。

## 适用边界

VCS Verdi Developer 的边界是刻意收窄的：

- 它聚焦于 Synopsys VCS 和 Verdi 工作流，不覆盖通用 RTL 开发或综合流程。
- 只有在真实 VCS/Verdi 流程确实运行后，才可以声称验证通过。
- 优先支持脚本化 non-GUI 验证，Verdi GUI 使用是可选能力，且取决于远程显示环境。
- 不应暴露真实 license 值、内部服务器细节、私有路径或私有基础设施信息。
- 它支持受控子集的 import、cocotb、coverage、URG、FSDB、evidence 和远程验证流程，而不是全部 Synopsys 特性。
- 本地验证资产保留在公开仓库和 release zip 之外；公开版本只发布经审查的 skill 运行时、参考资料和用户文档。

## 机构说明

Jiyuan Liu 和 He Li 隶属于东南大学电子科学与工程学院。
两位作者所在团队为东南大学电子科学与工程学院异构智能与量子计算实验室（HIQC课题组），相关工作面向异构智能、量子计算及相关计算系统研究。

## 联系方式

问题、合作或学术使用，请联系：[erie@seu.edu.cn](mailto:erie@seu.edu.cn)。

## 安全与敏感信息

- 不要提交真实 license 文件、secret token、私钥或内部主机信息。
- 像 `SNPSLMD_LICENSE_FILE` 这样的环境变量如果包含真实值，也应视为敏感信息。
- 在发布或 push 前，应对导入工件做敏感信息复核。

## 引用

本 skill 由东南大学电子科学与工程学院异构智能与量子计算实验室（HIQC课题组）相关作者维护。

如果本 skill 对你的研究、教学或工程流程有帮助，请引用。规范引用元数据以 [CITATION.cff](CITATION.cff) 为准。

```bibtex
@software{liu_2026_vcs_verdi_developer,
  author       = {Jiyuan Liu and He Li},
  title        = {{VCS Verdi Developer}: An Agent Skill for Synopsys VCS and Verdi Workflows},
  year         = {2026},
  version      = {0.5.4},
  date         = {2026-07-02},
  url          = {https://github.com/Eriemon/vcs-verdi-developer},
  license      = {Apache-2.0},
  note         = {Agent skill package for Synopsys VCS simulation, project import, cocotb planning, FSDB tooling, evidence gating, coverage diagnostics, and Verdi waveform-debug workflows}
}
```

## 许可证

Apache License 2.0，详见 [LICENSE](LICENSE)。
