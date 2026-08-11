# Hagency Kit

语言：简体中文 | [English](README.md)

实用 Agent 技能，用于审阅、诊断和维护 AI 辅助工程工作。

## Hagency CLI

`hgc` CLI 用于管理 Hagency workspace、source、skill discovery 和安装、profile，以及生成的 profile skill 输出。Source registry 位于 [`hagency-config.toml`](hagency-config.toml)，profile config 位于 `profiles/<name>/config.toml`。

```sh
uv tool install -e tools/hagency-cli
hgc s add <git-url> --sync
hgc s sync --profile <profile>
hgc skill ls
hgc skill add <skill>
hgc skill add <skill> -p <xxx>/skills
hgc skill add <source>:<selector> -d <workspace>
hgc skill add <source>:<selector> --global
hgc p init -p <xxx>/skills <profile>
hgc p init -d <workspace> <profile>
```

可为当前 shell 安装补全，或输出指定 shell 的补全脚本：

```sh
hgc --install-completion
hgc --show-completion bash
```

补全覆盖命令、别名、选项、目录，以及本地可用的 source、profile、skill 和 selector 值。它会尊重当前目录、`--root` 和 `--checkout-dir`；workspace 缺失、配置损坏、目录不可读或 source 未同步时会静默省略动态候选。

`[defaults].depth` 设置默认 sync 深度；临时性的 Git 网络失败会自动重试。失败后可用 `hgc source sync -s <slice>` 继续同步指定 source 范围。Git URL 推断出的 repo 名已存在时，`source add` 会 fallback 到 `owner/repo`；也可以传 `--name` 自定义 source 名。

同一份配置需要在不同平台使用不同 checkout 目录时，可以添加 Windows 覆盖值：

```toml
[defaults]
checkout_dir = "~/Projects/references"
checkout_dir_windows = "/d/Projects/references"
```

Checkout 目录的优先级是 `--checkout-dir`，然后是原生 Windows 上的 `checkout_dir_windows`，最后是 `checkout_dir`。WSL 不属于原生 Windows，仍使用 `checkout_dir`。在原生 Windows 上，Git Bash 路径 `/d/Projects/references` 会被规范化为 `D:/Projects/references`。如果未设置 `checkout_dir_windows`，原生 Windows 会回退到 `checkout_dir`。这是配置覆盖，不会新增 CLI 参数。

常规同步会拒绝非快进更新。如果上游 source 重写历史，且该 checkout 可丢弃，可用 `--reanchor` 重试失败的选择范围。重锚仅接受没有暂存、未暂存或未跟踪更改的 checkout，并用拉取到的上游历史替换 local-only commits。该选项支持 source 名、`--profile` 和 `--slice`；`--dry-run` 只描述满足条件时的行为。

`skill add` 通过唯一 skill 名或精确的 `SOURCE:selector` 安装一个已发现的 skill。默认链接到调用目录的 `.agents/skills`。`-p/--path` 直接指定最终 skills 容器，命令只会在其后追加 skill 名；`-d/--dir` 指定 workspace 根目录，安装目标为 `<workspace>/.agents/skills`；`--global` 安装到 `~/.agents/skills`。这三个选项互斥。相对目标路径以调用目录为基准，`~` 展开为当前用户的主目录。`--root` 和 `--checkout-dir` 只影响 skill discovery，不会改变安装目标。非 Windows 平台使用 symlink，Windows 使用 junction。

`profile init` 必须且只能选择一个目标：`-p/--path` 是最终 skills 容器，`-d/--dir` 是 workspace 根目录，对应目标为 `<workspace>/.agents/skills`。目标路径同样以调用目录为基准并支持 `~` 展开，`--root` 和 `--checkout-dir` 仍只用于 discovery。这一版本更改了 `-p` 的原有语义：请将 `hgc p init -p <root> <profile>` 改为 `hgc p init -d <root> <profile>`。

## Skills

| Skill | 适用场景 | 作用 |
| --- | --- | --- |
| [`analyze-diff`](skills/analyze-diff/SKILL.md) | 解释 git diff、提交范围、分支对比或粘贴的变更集 | 把原始变更证据整理成面向发布的摘要、功能变更列表、风险说明、测试缺口和发布说明草稿。 |
| [`diagnose-ai-workflow`](skills/diagnose-ai-workflow/SKILL.md) | 审计 prompt、Agent 工作流、工具链、多 Agent 系统或生产就绪度 | 基于现有证据，从 prompt、上下文、工具、架构、安全、可靠性和系统性能等维度评估工作流健康度。 |
| [`hagency-cli`](skills/hagency-cli/SKILL.md) | 使用 Hagency Kit CLI 管理 source、profile、skill discovery 或安装，以及 profile 初始化 | 帮助 Agent 检查和管理 Hagency workspace source、直接安装的 skill、profile skill selector、source sync，以及生成的 profile skill 输出。 |
| [`log-analyzer`](skills/log-analyzer/SKILL.md) | 调查应用、服务器、JSON、CI 或轮转 gzip 日志 | 通过采样和分析日志解释故障、错误峰值、慢请求、流量模式和事故信号，同时控制证据范围并做脱敏处理。 |

## Profiles

Profile 是用于 Agent 工作流场景的轻量级捆绑定义。

Profile 在 `profiles/<name>/config.toml` 中声明要启用的 source 名和 skill selector。初始化后，选中的 skills 会被物化到指定的 skills 容器；`-d/--dir` 使用 workspace 下的 `.agents/skills` 容器。
