# Changelog

## 0.1.16 (2026-04-15)

### Added
- `--remote` flag on `start`: CCR remote control via `control_request` — injects a control request after startup and prints session URL for connecting via the Claude mobile app. Composes with `--foreground`. (#067, D85)
- Unified thread primitive: `.cwork/threads/<id>.jsonl` + `index.json`, plus `thread_store.py` and `claude-worker thread` CLI (create/send/read/list/close). (#074-#080, D80)
- `--foreground` flag on `start` for systemd `Type=simple` deployments. (#053, D58)
- PM autonomous work loop: Mode 1 / Mode 2 scheduling via Claude Code. (#064, D65)
- Designer identity for requirements gathering. (#049, D70)
- Right-Hand Claude (RHC) identity for cross-project coordination.
- `metadata.json` written to archived worker directories. (#060, D69)
- Deterministic migration system (`claude-worker migrate`). (#061, D66)
- Identity periodic tasks via `periodic.yaml` with manager polling.
- Identity-specific hooks via `hooks.json` merge.
- Per-identity `config.yaml` supporting `claude_args` and `env`.
- Compaction resilience: warnings, detection via `compact_boundary`, and identity re-injection on resume. (#055, D59)
- 70% context threshold: finish current task, then wrap up.
- Wrap-up procedure injection at 80% context threshold. (#054, D61)
- `cairn validate` enforcement inside the commit checker. (#063, D68)
- Off-hours idle cron for the PM identity.
- Post-completion reporting in TL identity. (D57)
- Commit log + TodoWrite workflow instructions. (D72, D73)
- S2 design discussion protocol. (#065, D71)
- Skeleton scaffolding materialized from identity directories.
- Standard discoverability commands: `version` / `--version`, `changelog [--since V]`, `docs`, `skill`. (#071, D86)

### Fixed
- `start --resume` archives stale dead worker directories instead of reusing them. (#068, D82)
- Legacy `/tmp/` paths in saved `claude_args` are auto-fixed on resume. (#069, D83)
- Resume falls back to the latest archive when the active directory is missing. (#070, D84)
- D80 collision: renamed CCR remote control to D81 before reassigning D80 to the thread primitive.
- `send` flag ordering: trailing flags are no longer swallowed by positional args. (#058, D64)
- Compaction detection now keys on `compact_boundary` rather than `init`. (#055, D59)
- Hardened `replaceme` against silent failures. (#052, D56)

### Changed
- Identity runtime directories renamed to `.cwork/roles/`. (#062, D67)
- PM backlog continues processing during conversations. (D62)
- Bumped `claugs` floor to `>=0.6.8` for sub-agent context fix. (D60)

### Docs
- Documentation audit: README, CLAUDE.md, architecture.md. (#056, D63)
- Lifecycle tag definition added to project GVP library.
- Merged approved GVP elements: V6, P10, D53, OBS1, plus G2/V4 modifications.
- Inbox/threads design: P11-P12, D74-D79 and global P8-P9, OBS3-OBS4.
- Backfill of 7 missing decisions (D46-D52) alongside the compaction detector hook.

## 0.1.0 (initial)

- Initial claude-worker release: launch and communicate with Claude Code subprocess workers, FIFO-based stdin bridge, per-worker log, status detection, REPL, `send` / `read` / `wait-for-turn` primitives, install-hook for SessionStart UUID env injection.
