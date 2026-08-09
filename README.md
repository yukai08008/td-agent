# TD Agent

A persistent, interactive Agent CLI based on the TOE-DAC control protocol — Target, Observe, Estimate, Decide, Act, and Check.

TD Agent is a proof of concept for long-running Agent tasks. A clear user requirement becomes a **User Thread**; every return to that requirement creates a new **Session**; planning and execution are represented by persistent, traceable **TD instances**.

## Quick Start

Requirements: Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/yukai08008/td-agent.git
cd td-agent

cp .env.example .env.local
chmod 600 .env.local
# Edit .env.local and add at least one API key referenced by config/models.json.

uv sync
uv run toe-dac doctor
uv run toe-dac new
```

## Usage

```bash
# Create a User Thread for a new requirement and open its first Session
uv run toe-dac new

# Continue the latest unfinished requirement in a new Session
uv run toe-dac

# Continue a specific User Thread
uv run toe-dac continue --thread ut_xxxxxxxx

# Inspect Threads and Sessions
uv run toe-dac threads
uv run toe-dac sessions --thread ut_xxxxxxxx

# Check the effective local setup
uv run toe-dac config
uv run toe-dac doctor

# Show all commands
uv run toe-dac --help
```

Inside an interactive Session, use `/help` to list commands such as `/status`, `/history`, `/pause`, `/resume`, `/cancel`, and `/quit`.

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
├── messages.jsonl
├── sessions/
│   ├── ss_xxxxxxxx.json
│   └── ss_xxxxxxxx.json
└── td/
    └── td_xxxxxxxx/
        ├── state.json
        ├── event.jsonl
        └── operation.jsonl
```

- A new requirement creates a new User Thread.
- Reopening a requirement creates a Session attached to that Thread.
- TD instances describe the requirement's planning and execution hierarchy.
- Reaching a terminal TD state never silently turns the Thread into a container for another requirement.

## Project Structure

```text
td-agent/
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
│   ├── llm/                # Local OpenAI-compatible model clients
│   └── e2e/                # Executable proof-of-concept scenarios
├── tests/
├── pyproject.toml
└── README.md
```

## E2E Scenarios

```bash
# List executable scenarios
uv run toe-dac case list

# Run deterministic mock scenarios
uv run toe-dac --data ./data run LIVE-001 --mode mock
uv run toe-dac --data ./data run LIVE-002 --mode mock
uv run toe-dac --data ./data run LIVE-006 --mode mock

# Resume a scenario waiting for human input
uv run toe-dac --data ./data resume <run_id>

# Inspect its report
uv run toe-dac --data ./data report <run_id>
```

## Development

```bash
uv sync
uv run pytest
uv build
```

## Current Scope

The POC currently covers persistent Threads and Sessions, the TOE-DAC state chain, deterministic structured validation, Target repair, Action/Target checks, recovery budgets, human interruption, and exception experience tracking.

The restricted Action Executor, parent/child TD orchestration, evidence capture, and semantic experience retrieval are still under development. The interactive flow therefore stops at the Executor boundary after a Plan is accepted.
