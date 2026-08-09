# Changelog

All notable changes to TD Agent are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Reserved for changes that have not yet been released.

## [0.2.0] - 2026-08-09

### Added

- One-line install, update, and uninstall for Linux and macOS.
- Chinese documentation in `README_zh.md`.
- `toe-dac --version` and `toe-dac upgrade` commands.
- Configurable startup update checks with cache and network timeout.
- Machine-local installed configuration under `~/.config/td-agent/`.
- Installed runtime data default under `~/.local/share/td-agent/`.
- Embedded minimal declarative state-machine runtime for public standalone installation.
- `toe-dac changelog` terminal release-note viewer.

### Changed

- Reworked README around install, uninstall, update, usage, and architecture.
- Removed the private local-path `andy-state` dependency.
- Declared `pydantic` as a direct runtime dependency.

### Security

- Kept model API keys in ignored `.env.local` files and model configuration limited to `apiKeyEnv` references.
- Verified source distributions and wheels do not contain local secrets.

## [0.1.0] - 2026-08-09

### Added

- Persistent User Threads, Sessions, TD state, messages, events, and operation logs.
- TOE-DAC Target, Observe, Estimate, Decide, Act, and Check state chain.
- Natural-language conversation controller with structured model outputs.
- Target validation, automatic repair, recovery budgets, and human escalation.
- Action Check and Target Check separation.
- Exception-treatment experience ledger with success and failure outcomes.
- Executable mock and live E2E proof-of-concept scenarios.
- Rich interactive terminal interface.

[Unreleased]: https://github.com/yukai08008/td-agent/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/yukai08008/td-agent/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/yukai08008/td-agent/releases/tag/v0.1.0
