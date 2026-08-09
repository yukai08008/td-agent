# TD Agent

[中文文档](README_zh.md)

A persistent, interactive Agent CLI based on the TOE-DAC control protocol — Target, Observe, Estimate, Decide, Act, and Check.

TD Agent is a proof of concept for long-running Agent tasks. A clear user requirement becomes a **User Thread**; every return to that requirement creates a new **Session**; planning and execution are represented by persistent, traceable **TD instances**.

## One-line Install

Linux and macOS:

```bash
curl -fsSL https://raw.githubusercontent.com/yukai08008/td-agent/main/install.sh | bash
```

The installer installs [uv](https://docs.astral.sh/uv/) when needed, installs TD Agent as an isolated uv tool, and initializes machine-local configuration in `~/.config/td-agent/`.

After installation, run:

```bash
toe-dac
```

At startup, the CLI checks that at least one model is enabled and has an API key. If none is usable, it opens an interactive setup guide. Secrets are saved only to the machine-local `~/.config/td-agent/.env.local`.

## Uninstall

```bash
curl -fsSL https://raw.githubusercontent.com/yukai08008/td-agent/main/install.sh | bash -s -- uninstall
```

Configuration and local runtime data are preserved.

## Update

```bash
toe-dac upgrade
```

Or run the installer explicitly:

```bash
curl -fsSL https://raw.githubusercontent.com/yukai08008/td-agent/main/install.sh | bash -s -- update
```

TD Agent checks the remote version at startup and only prints a notice when a newer version is available. The check uses a local cache and a short network timeout.

Control it in `.env.local`:

```dotenv
TOE_DAC_UPDATE_CHECK=true
TOE_DAC_UPDATE_CHECK_INTERVAL=86400
TOE_DAC_UPDATE_CHECK_TIMEOUT=1.5
```

Set `TOE_DAC_UPDATE_CHECK=false` to disable it.

## Run a specific version

The installed CLI runs normally by default. To temporarily run an exact tagged release without replacing it:

```bash
toe-dac --use-version 0.2.0 --version
TOE_DAC_DATA=~/.td-agent/versions/v0.2/data toe-dac --use-version 0.2.0 new
```

To permanently install an exact release:

```bash
toe-dac upgrade --version 0.2.0
curl -fsSL https://raw.githubusercontent.com/yukai08008/td-agent/main/install.sh | bash -s -- install 0.2.0
```

Tagged standalone execution is supported from `v0.2.0`. Set `TOE_DAC_DATA` only when testing a release with an incompatible persisted-state format.

## Current release

The current source version is `v0.6.0`. Every TOE-DAC stage retains model judgment, while a deterministic control plane handles canonical file persistence, evidence registration, protocol normalization, hard checks, and bounded runtime recovery.

See [模型判断与确定性控制边界](docs/model-control-boundary.md) for the stage-by-stage contract.

[Complete version record](versions.md) · [GitHub Releases](https://github.com/yukai08008/td-agent/releases)

## Install with uv directly

```bash
uv tool install git+https://github.com/yukai08008/td-agent.git
```

When bypassing `install.sh`, create `~/.config/td-agent/models.json` and `~/.config/td-agent/.env.local` yourself from the repository templates.

## Usage

```bash
# Show version and system information
toe-dac --version

# Create a User Thread for a new requirement and open its first Session
toe-dac new

# Reattach the latest persistent Session
toe-dac

# Reattach the latest Session in a specific User Thread
toe-dac continue --thread ut_xxxxxxxx

# Reattach an exact Session
toe-dac continue --session ss_xxxxxxxx

# Explicitly start another Session for the same requirement
toe-dac session new --thread ut_xxxxxxxx

# Inspect Threads and Sessions
toe-dac threads
toe-dac sessions --thread ut_xxxxxxxx

# Check the effective local setup
toe-dac config
toe-dac config --show
toe-dac doctor

# Upgrade to the latest GitHub version
toe-dac upgrade

# Show all commands
toe-dac --help
```

Inside an interactive Session, use `/help` to list commands such as `/status`, `/why`, `/show`,
`/continue`, `/reobserve`, `/replan`, `/history`, `/pause`, `/resume`, `/cancel`, and `/quit`.
Read-only control commands never advance the TD. `/quit` only detaches the CLI; it does not end the Session.

## Skills and persona

Every Session attachment initially loads only `~/.td-agent/skills/index.md` and the active blue/green system
prompt from `~/.td-agent/persona/active.json`. A relevant Claude-style `SKILL.md` is loaded progressively;
allow-listed skill tools are exposed only in their declared phases and within a per-phase call budget.
Existing files are never overwritten by installation or initialization.

## Configuration

Environment values are loaded with this precedence:

```text
process environment > .env.local > .env > .env.example
```

- `.env.local` contains machine-specific secrets and is never committed.
- `.env` contains committed project-wide placeholders or non-secret defaults.
- `.env.example` documents every supported variable.
- `config/models.json` contains model metadata and `apiKeyEnv` references, never inline API keys.

Each machine should create its own `.env.local`. Do not copy GitHub credentials, SSH private keys, or local runtime data between machines.

Source checkouts load configuration from the project directory. One-line installations load it from `~/.config/td-agent/`.

## How It Works

The controller advances a task through six explicit stages:

```text
Target → Observe → Estimate → Decide → Act → Check
```

Each stage has one responsibility:

1. **Target** defines a verifiable result, exclusions, and acceptance criteria.
2. **Observe** records sourced facts and remaining unknowns.
3. **Estimate** evaluates feasibility, risk, cost, and information gaps.
4. **Decide** creates an action graph with dependencies, assertions, and budgets.
5. **Act** executes one atomic action and records structured evidence.
6. **Check** distinguishes action completion from actual target achievement.

The model proposes structured outputs; deterministic validation and the state machine decide whether a transition is accepted. Invalid outputs are logged, repaired within budget, and escalated to a human when automatic recovery cannot continue.

## Persistence Model

```text
User Thread (one explicit requirement)
├── meta.json
├── state.json
├── logs/
│   ├── event.jsonl
│   └── opr.jsonl
├── trace/sessions/
│   └── sess-xxxxxxxx-YYYYMMDD_HHMMSS/
│       ├── session.json
│       ├── messages.jsonl
│       ├── evidence.jsonl
│       └── screenshots/
├── artifacts/<td_id>/
└── td/<td_id>/state.json
```

- A new requirement creates a new User Thread.
- Reopening a requirement creates a Session attached to that Thread.
- TD instances describe the requirement's planning and execution hierarchy.
- `/evidence` only opens the current Session directory under `trace/sessions/`; it never creates, copies, aggregates, or refreshes evidence.
- Reaching a terminal TD state never silently turns the Thread into a container for another requirement.
- `toe-dac storage migrate` performs a read-only migration preview; add `--execute` only after reviewing it.
- Legacy `data/threads/` remains readable and is retained after a verified migration.

## Project Structure

```text
td-agent/
├── install.sh             # One-line install, update, and uninstall
├── config/                 # Safe model registry and example config
├── docs/                   # Protocol, state-machine, and E2E designs
├── src/toe_dac/
│   ├── cli.py              # CLI command routing
│   ├── chat_ui.py          # Rich interactive terminal UI
│   ├── conversation.py     # Natural-language conversation controller
│   ├── events.py           # UI-neutral conversation events
│   ├── service.py          # Persistent TD state-machine service
│   ├── storage.py          # Thread, Session, TD, message, and log storage
│   ├── experience.py       # Exception-treatment experience ledger
│   ├── llm_adapter.py      # Structured model adapter
│   ├── update_check.py     # Cached remote version check
│   ├── llm/                # Local OpenAI-compatible model clients
│   └── e2e/                # Executable proof-of-concept scenarios
├── tests/
├── pyproject.toml
├── README.md
└── README_zh.md
```

## E2E Scenarios

标准回归规范见 [docs/standard-regression-tests.md](docs/standard-regression-tests.md)。
性能对比基线见 [docs/benchmarks/](docs/benchmarks/README.md)。

```bash
# List executable scenarios
uv run toe-dac case list

# Run deterministic mock scenarios
uv run toe-dac run REG-001 --mode mock
TOE_DAC_DATA=./data uv run toe-dac run LIVE-001 --mode mock
TOE_DAC_DATA=./data uv run toe-dac run LIVE-002 --mode mock
TOE_DAC_DATA=./data uv run toe-dac run LIVE-006 --mode mock

# Resume a scenario waiting for human input
TOE_DAC_DATA=./data uv run toe-dac resume <run_id>

# Inspect its report
TOE_DAC_DATA=./data uv run toe-dac report <run_id>

# Run the standard browser/model regression before release
toe-dac run REG-001 --mode live \
  --model deepseek-v4-flash \
  --model-config ~/.config/td-agent/models.json
```

## Development

```bash
git clone https://github.com/yukai08008/td-agent.git
cd td-agent
cp .env.example .env.local
chmod 600 .env.local

uv sync
uv run pytest
uv build
```

## Current Scope

The POC currently covers persistent Threads and Sessions, the TOE-DAC state chain, deterministic structured validation, Target repair, a bounded `agent_response` Executor, Action/Target checks, recovery budgets, human interruption, progressive skills, and exception experience tracking.

Executors that mutate files, run commands, or operate remote systems remain restricted and stop at an explicit boundary. Parent/child TD orchestration, richer evidence capture, and semantic experience retrieval are still under development.
