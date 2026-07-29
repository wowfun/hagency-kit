# Hagency Kit

语言：简体中文 | [English](README.md)

实用 Agent 技能，用于审阅、诊断和维护 AI 辅助工程工作。

## Hagency CLI

`hagency` CLI 用于管理 Hagency workspace、source、skill discovery 和安装、profile，以及生成到 `.agents/skills` 的 profile 输出；`hgc` 是它的短别名。Source registry 位于 [`hagency-config.toml`](hagency-config.toml)，profile config 位于 `profiles/<name>/config.toml`。

```sh
uv tool install -e tools/hagency-cli
hgc s add <git-url> --sync
hgc s sync --profile <profile>
hgc skill ls
hgc skill add <skill>
hgc skill add <source>:<selector> --global
hgc p init -p <target> <profile>
```

`[defaults].depth` 设置默认 sync 深度；临时性的 Git 网络失败会自动重试。失败后可用 `hagency source sync -s <slice>` 继续同步指定 source 范围。Git URL 推断出的 repo 名已存在时，`source add` 会 fallback 到 `owner/repo`；也可以传 `--name` 自定义 source 名。

常规同步会拒绝非快进更新。如果上游 source 重写历史，且该 checkout 可丢弃，可用 `--reanchor` 重试失败的选择范围。重锚仅接受没有暂存、未暂存或未跟踪更改的 checkout，并用拉取到的上游历史替换 local-only commits。该选项支持 source 名、`--profile` 和 `--slice`；`--dry-run` 只描述满足条件时的行为。

`skill add` 通过唯一 skill 名或精确的 `SOURCE:selector` 安装一个已发现的 skill。默认链接到调用目录的 `.agents/skills`，带 `--global` 时链接到当前用户的 `~/.agents/skills`。非 Windows 平台使用 symlink，Windows 使用 junction。

## Skills

| Skill | 适用场景 | 作用 |
| --- | --- | --- |
| [`analyze-diff`](skills/analyze-diff/SKILL.md) | 解释 git diff、提交范围、分支对比或粘贴的变更集 | 把原始变更证据整理成面向发布的摘要、功能变更列表、风险说明、测试缺口和发布说明草稿。 |
| [`diagnose-ai-workflow`](skills/diagnose-ai-workflow/SKILL.md) | 审计 prompt、Agent 工作流、工具链、多 Agent 系统或生产就绪度 | 基于现有证据，从 prompt、上下文、工具、架构、安全、可靠性和系统性能等维度评估工作流健康度。 |
| [`hagency-cli`](skills/hagency-cli/SKILL.md) | 使用 Hagency Kit CLI 管理 source、profile、skill discovery 或安装，以及 profile 初始化 | 帮助 Agent 检查和管理 `hagency` workspace source、直接安装的 skill、profile skill selector、source sync，以及生成的 profile skill 输出。 |
| [`log-analyzer`](skills/log-analyzer/SKILL.md) | 调查应用、服务器、JSON、CI 或轮转 gzip 日志 | 通过采样和分析日志解释故障、错误峰值、慢请求、流量模式和事故信号，同时控制证据范围并做脱敏处理。 |

## Profiles

Profile 是用于 Agent 工作流场景的轻量级捆绑定义。

Profile 在 `profiles/<name>/config.toml` 中声明要启用的 source 名和 skill selector。初始化后，选中的 skills 会被物化到目标 workspace 的 `.agents/skills` 下。
