# Hagency Kit

语言：简体中文 | [English](README.md)

实用 Agent 技能，用于审阅、诊断和维护 AI 辅助工程工作。

## Skills

| Skill | 适用场景 | 作用 |
| --- | --- | --- |
| [`analyze-diff`](skills/analyze-diff/SKILL.md) | 解释 git diff、提交范围、分支对比或粘贴的变更集 | 把原始变更证据整理成面向发布的摘要、功能变更列表、风险说明、测试缺口和发布说明草稿。 |
| [`diagnose-ai-workflow`](skills/diagnose-ai-workflow/SKILL.md) | 审计 prompt、Agent 工作流、工具链、多 Agent 系统或生产就绪度 | 基于现有证据，从 prompt、上下文、工具、架构、安全、可靠性和系统性能等维度评估工作流健康度。 |
| [`eval-skill-quality`](skills/eval-skill-quality/SKILL.md) | 审阅 skill，或在发布前整理 skill | 评估 skill 质量、触发可靠性、语义清晰度、SRL 可靠性、泄漏风险、可维护性和实际价值。 |
| [`git-collab-flow`](skills/git-collab-flow/SKILL.md) | 管理 `dev`、`feat-*` / `dev-*` 和 `local-*` 分支工作流 | 生成安全的 git 命令序列，用于同步主线更新、变基功能分支、cherry-pick 可公开提交，并保持 PR 历史干净。 |
| [`log-analyzer`](skills/log-analyzer/SKILL.md) | 调查应用、服务器、JSON、CI 或轮转 gzip 日志 | 通过采样和分析日志解释故障、错误峰值、慢请求、流量模式和事故信号，同时控制证据范围并做脱敏处理。 |

## Profiles

Profile 是用于 Agent 工作流场景的轻量级捆绑定义。

外部 skill 来源统一登记在 [`hagency-config.toml`](hagency-config.toml)。每个 profile 在 `profiles/<name>/config.toml` 中声明要启用的 source；生成的 `.agents/skills/` 链接不进入 git。先同步 remote source，再把 profile 初始化到目标 workspace。

```sh
uv tool install -e tools/hagency-cli
hagency source sync --profile content --dry-run
hagency profile init -p ~/workspaces/content content --dry-run
```

`hagency-config.toml` 中的 `[defaults].depth` 用于设置 source sync 的默认深度；传入 `--depth` 时会覆盖它。使用 `hagency source add <git-url> --sync` 可以添加 source 后立即同步；如果推断出的 repo 名已存在，Git URL 会 fallback 到 `owner/repo`，也可以传 `--name` 自定义名称。Profile skill 管理可传 source 名或 skill 名；skill 名有歧义时会报错。`hagency source sync` 会自动重试临时性的 Git clone/fetch/pull 失败。如果某个 source 重试后仍失败，可以用 1-based index 同步子集，例如 `sync source [4/5] Waza` 失败后运行 `hagency source sync -s 4:`，或用 `-s 1,3:` 同步第 1 个加第 3 个及其之后的 source。
