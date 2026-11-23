# Hagency Kit

语言：简体中文 | [English](README.md)

实用 Agent 技能，用于审阅、诊断和维护 AI 辅助工程工作。

## Hagency CLI

`hgc` 用于管理 skill source、安装、profile、项目文件同步、构建产物清理和本地模型代理。在仓库根目录安装：

```sh
uv tool install -e tools/hagency-cli
hgc --help
hgc skill list --source workspace
hgc tool ls
hgc skill add workspace:skills/analyze-diff --dry-run
hgc skill add workspace:skills/analyze-diff
```

Skill 默认安装到调用目录的 `.agents/skills`；使用 `--dir /path/to/project` 指定其他项目，或用 `--global` 安装到 `~/.agents/skills`。Workspace 按 `--root`、当前目录及其父目录、editable 安装所用的 Hagency Kit 源码仓库顺序查找。Wheel 安装在 workspace 外运行时需要指定 `--root`。

`hgc skill ls` 每行输出一个技能引用，可直接传给 `hgc skill add`。在任意命令后加 `--help` 可查看用法和选项，用 `hgc --install-completion` 安装 shell 补全。配置和操作流程见 [CLI 指南（英文）](tools/hagency-cli/README.md#documentation)。

`hgc tool ls --profile dev` 运行该 profile 中各工具的版本命令，版本要求未满足时显示配置的范围。只查看配置时加 `--no-probe`。脚本可用 `--strict` 检查可用性，任一工具不可用或版本要求未满足时退出码为 1。用 `hgc profile i dev -T` 可在应用技能时一并安装工具。[工具命令与配置](tools/hagency-cli/docs/tools.md)

## Skills

| Skill | 适用场景 | 作用 |
| --- | --- | --- |
| [`analyze-diff`](skills/analyze-diff/SKILL.md) | 解释 git diff、提交范围、分支对比或粘贴的变更集 | 把原始变更证据整理成面向发布的摘要、功能变更列表、风险说明、测试缺口和发布说明草稿。 |
| [`diagnose-ai-workflow`](skills/diagnose-ai-workflow/SKILL.md) | 审计 prompt、Agent 工作流、工具链、多 Agent 系统或生产就绪度 | 基于现有证据，从 prompt、上下文、工具、架构、安全、可靠性和系统性能等维度评估工作流健康度。 |
| [`log-analyzer`](skills/log-analyzer/SKILL.md) | 调查应用、服务器、JSON、CI 或轮转 gzip 日志 | 通过采样和分析日志解释故障、错误峰值、慢请求、流量模式和事故信号，同时控制证据范围并做脱敏处理。 |

## Profiles

Profile 是用于 Agent 工作流场景的轻量级捆绑定义。

Profile 在 `profiles/<name>/config.toml` 中声明要启用的 source 名和 skill selector。应用 profile 后，选中的 skills 会被物化到指定的 skills 容器；`-d/--dir` 使用 workspace 下的 `.agents/skills` 容器。
