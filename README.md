# sub-agy

> Use **Antigravity** as a subagent in **Claude Code / Codex / Kimi Code**.

**English** | [简体中文](./README-CN.md)

![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)
![Runtime deps: stdlib only](https://img.shields.io/badge/runtime%20deps-stdlib%20only-lightgrey.svg)

`sub-agy` turns the Antigravity CLI (`agy`) into an **asynchronous code-execution backend** for your planning agent. The planner writes a plan and reviews results; the heavy lifting runs in the background on your Gemini quota, inside an isolated git worktree, and comes back as a structured acceptance report.

## How it works

```mermaid
flowchart LR
    P["Plan<br/>Claude Code · Codex · Kimi Code"] -- "sub-agy run" --> S["Detached supervisor<br/>(FIFO slot queue)"]
    S --> W["agy · Gemini<br/>isolated git worktree"]
    W -- "--json-schema" --> R["result.json<br/>structured acceptance"]
    R -- "watcher wakes the agent" --> V{Review}
    V -- pass --> M["git merge agy/&lt;job-id&gt;<br/>(always human)"]
    V -- fail --> F["feedback<br/>same conversation, next round"] --> S
```

## Features

- **Async dispatch, background execution** — `run` returns a `job_id` immediately; a detached supervisor keeps `agy` going even after the caller exits. Jobs beyond `max_concurrent` queue up FIFO.
- **git worktree isolation** — every job runs on its own branch (`agy/<job-id>`) in its own worktree; your main branch stays clean.
- **Structured acceptance contract** — `--json-schema` makes `agy` report `summary` / `files_changed` / `tests_passed` and more.
- **Proactive completion notification** — one background watcher per job: whichever job finishes first gets reviewed first. A Stop-hook safety net (`sub-agy pending`) catches anything left unharvested.
- **Automatic feedback loop** — failed acceptance triggers `feedback`, which keeps the conversation and starts the next repair round.
- **0-token quota check** — `quota` reads the Antigravity 5h/weekly windows for free.

## Requirements

- **agy CLI ≥ 1.1.8**, logged in once interactively (run `agy` once to complete OAuth)
- **Python ≥ 3.11**
- **uv** (recommended) or pipx
- **git** (needed for worktree isolation; non-git projects fall back to in-place execution)
- **Claude Code, Codex desktop, or Kimi Code CLI** (at least one)

## Installation

> **The CLI is required — plugins alone do not work.** The Claude Code / Codex / Kimi Code plugins are thin orchestration layers: every plugin command shells out to the `sub-agy` binary and fails with exit 127 if it is missing. If you prefer not to install it globally, the only fallback is cloning the repo and setting `export SUB_AGY_HOME=<repo path>` — the plugins then run it from source via `uv run --project`, which still requires uv.
>
> Since v0.1.1, `/subagy:doctor` detects a missing CLI and offers to install it for you (one confirmation, then `uv tool install`). The `agy` binary and its OAuth login always remain manual.

### CLI (no clone needed)

```bash
uv tool install git+https://github.com/Besty0728/sub-agy
```

Verify:

```bash
sub-agy doctor
```

### Claude Code plugin

```text
/plugin marketplace add Besty0728/sub-agy
/plugin install subagy@subagy
/reload-plugins
```

### Codex desktop

No clone needed — add a git marketplace to `~/.codex/config.toml`:

```toml
[marketplaces.subagy]
source_type = "git"
source = "https://github.com/Besty0728/sub-agy"
```

Restart Codex and install `subagy` from the plugin panel.

For offline/development use, a local marketplace works too:

```toml
[marketplaces.subagy]
source_type = "local"
source = "<path to clone>"
```

### Kimi Code CLI

```text
/plugins install https://github.com/Besty0728/sub-agy
/reload
```

Commands are namespaced: `/subagy:dispatch`, `/subagy:harvest`, etc. After dispatch, each job is watched by a background `subagy-watcher` subagent that returns to the main agent the moment the job finishes. For local development use `/plugins install <path to clone>` (the plugin is copied to `$KIMI_CODE_HOME/plugins/managed/`; reinstall after editing sources).

## Configuration (optional)

sub-agy runs fine with zero configuration. To change defaults, create `~/.config/sub-agy/config.toml` (override the location with the `SUB_AGY_CONFIG` environment variable). Every key is optional; the values below are the built-in defaults:

```toml
default_model = "gemini-3.7-flash"  # model slug passed to agy
default_effort = "medium"           # low | medium | high
default_timeout = "30m"             # per-job timeout, Go duration (Ns/Nm/Nh)
max_concurrent = 3                  # jobs running at once per project; extras queue FIFO
max_retries = 1                     # extra attempts after a timeout
queue_timeout = "2h"                # max wait for a run slot before the job errors
agy_bin = "agy"                     # default agy binary name or path (used when accounts is not set)
low_quota_threshold = 20.0          # low-quota avoidance threshold (default 20%)
```

### Multi-account configuration (opt-in)

If you have multiple independently authenticated `agy` launchers (e.g. personal, work, secondary accounts), configure them under `[[accounts]]` (or `[accounts.<name>]` tables):

```toml
# Example multi-account setup with 3 independent profiles:
[[accounts]]
name = "default"
agy_bin = "~/.local/bin/agy"
brain_dir = "~/.gemini/antigravity-cli/brain"  # optional; defaults to standard brain path

[[accounts]]
name = "rainy"
agy_bin = "~/.local/bin/agy-rainy"
brain_dir = "~/.gemini-rainy/antigravity-cli/brain"

[[accounts]]
name = "rainy-2"
agy_bin = "~/.local/bin/agy-rainy-2"
brain_dir = "~/.gemini-rainy-2/antigravity-cli/brain"
low_quota_threshold = 25.0                    # optional per-account override
```

Key behaviors:
- **0-token automatic selection**: For each new `run`, `sub-agy` queries the configured accounts' Gemini quotas (0 model tokens), checks for usable five-hour AND weekly headroom above the low-quota threshold (default 20%), and selects the account with the best headroom.
- **Fail-safe determinism**: If quota queries fail or every account is below the threshold, `sub-agy run` fails with a clear deterministic error rather than silently using an exhausted profile.
- **Explicit account override**: Use `sub-agy run --account <name>` to manually pick an account (returns a clear error if the name is unknown).
- **Per-job selection & feedback stickiness**: Account choice happens **once per new job**, before the detached supervisor starts. Selection **never switches mid-conversation**: all subsequent `feedback` rounds stick to the job's original account to preserve conversation state and context. Old jobs without account metadata continue using the legacy default account.
- **Transcript recovery**: Transcript recovery reads the job's chosen account brain directory, avoiding cross-account transcript confusion.
- **Backwards compatibility**: If no `accounts` are configured, single-account installs and `agy_bin` continue working exactly as before.

A missing file simply means the defaults above. Per-job CLI flags (`--model` / `--effort` / `--timeout` / `--account`) override the config.

## Quick start

### 1. Write a plan file

```bash
cat > plan.md <<'EOF'
---
scope: [src/**/*.py]
acceptance:
  - pytest tests/ -q passes
constraints:
  - no new dependencies
---
Add type annotations to the login function and fix the type errors this exposes.
EOF
```

### 2. Dispatch

In Claude Code (the main agent defaults to `gemini-3.7-flash` + `medium` effort, auto-raising to `high` for complex plans and lowering to `low` for trivial ones):

```text
/subagy:dispatch plan.md
```

Or straight from the CLI (with explicit `--effort` / `--model` if you like):

```bash
sub-agy run --plan plan.md --cwd ./my-project
```

### 3. Automatic review

When a watcher wakes the main agent, it reviews the result following the `/subagy:harvest` rules: accept, fail, or send it back for another round.

### 4. Merge

Once acceptance passes, merge the execution branch yourself:

```bash
git merge agy/<job-id>
```

### Quota check

```bash
sub-agy quota --oneline
```

Sample output (currently localized in Chinese):

```
Gemini 模型：5h 限额剩余 99.8%（32分钟后重置），7d 限额剩余 99.8%（6天11小时后重置）；Claude/GPT 模型：7d 限额剩余 100.0%
```

## CLI reference

| Command | Description |
|---|---|
| `sub-agy run --plan <plan.md> [--account <name>]` | Dispatch a plan, returns `job_id` immediately |
| `sub-agy status [--all]` | Show job status, queue position, tokens, elapsed |
| `sub-agy result <job-id>` | Harvest the structured result of a finished job |
| `sub-agy feedback <job-id> "..."` | Send feedback, start the next repair round |
| `sub-agy watch <job-id>` | Block until the job reaches a terminal state |
| `sub-agy cancel <job-id>` | Cancel a job |
| `sub-agy list` | List jobs in the current project |
| `sub-agy pending [--under <dir>]` | List finished-but-unharvested jobs (data source for the Stop-hook safety net) |
| `sub-agy cleanup <job-id>` | Remove the job worktree and branch |
| `sub-agy quota [--account <name>] [--oneline] [--pretty]` | Query Antigravity quota (0 tokens) |
| `sub-agy doctor` | Environment diagnostics |

## Security & compliance

- **Official-CLI process orchestration only** — sub-agy never touches model APIs and never stores or proxies API keys.
- **Fully autonomous + worktree isolation + human merge** — `agy` always starts with `--dangerously-skip-permissions` for unattended execution; the safety boundary is the isolated git worktree, the plan's scope/constraints, and the fact that `git merge` is always triggered by a human.
- **Automatic feedback, manual merge** — `feedback` re-runs with full context preserved; `git merge agy/<job-id>` is always yours to run.

## License

[MIT](./LICENSE)
