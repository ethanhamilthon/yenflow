# AGENTS.md — Yenflow

Operating guidelines and strict invariants for AI agents working in this repository.

## 1. Project Location & Scope
- **Working Directory**: Strictly `/Users/erdana/pi/yenflow` (or subdirectories of `~/pi`).
- **Internal Core**: Private TOML-driven Flow I2V batch runner (`src/gflow_cli/flow_batch.py`). Do not expose new public Click CLI commands or MCP tools.

## 2. Project Defaults
- **Video Model**: `omni_flash` (Omni 1.1 Flash)
- **Resolution**: `360p` (native Flow resolution)
- **Aspect Ratio**: `16:9` (`landscape`)
- **Clip Duration**: 4s per scene

## 3. Silent Background Execution
- Always use `headless=True` via `FlowApiClient(background=True)`.
- Never open visible Chrome windows or activate `Google Chrome.app` on macOS.
- User focus must never be stolen from their active application.

## 4. Credit & Execution Safety
- **Two-Phase Credit Lock**: Generating paid video requires preparing `plan.json` (calculating credits and SHA-256 hash) and verifying matching `approval.json` (`{"approved": true, "plan_sha256": "..."}`).
- **Idempotent Reconciliation**: Checkpoints are persisted before submit (`submit_attempted`) and upon handle discovery (`remote_started`). If a connection drops, poll by `media_id` — never blindly resubmit.
- **Queue Limits**: Max 10 queued, max 5 active (FIFO). Pause/cancel applies only to pending scenes.
- **Technical Retries**: At most 1 automatic retry on preflight errors; creative retries require explicit approval.

## 5. Quality & Verification Gates
Before committing any changes, run:
```bash
.venv/bin/ruff check src/gflow_cli tests
.venv/bin/ruff format --check src/gflow_cli tests
.venv/bin/pyright --pythonpath .venv/bin/python src/gflow_cli
.venv/bin/pytest -q tests/auth/test_verification.py tests/api/test_video_request.py tests/api/test_client_launch_kwargs.py tests/api/transports/test_ui_automation_video.py tests/api/transports/test_migrated_composer.py tests/test_flow_batch.py
.venv/bin/gflow doctor
```
All checks must pass with 0 errors/warnings.
