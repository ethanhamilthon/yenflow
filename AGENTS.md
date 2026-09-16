# AGENTS.md — Yenflow

Operating guidelines and strict invariants for AI agents working in this repository.

## 1. Project Location & Scope
- **Working Directory**: Project root (`yenflow`).
- **MCP Core**: Stateless Model Context Protocol (MCP) server for Google Flow (Veo video and Imagen image generation). All state is held by the MCP client.

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
- **Two-Phase Credit Lock**: Generating paid video requires preparing a plan (calculating credits and SHA-256 hash) and verifying matching client approval (`{"approved": true, "plan_sha256": "..."}`).
- **Stateless Orchestration**: The MCP client orchestrates and tracks scenes, media IDs, and batch state. The server remains stateless and requires no local database.
- **Queue Limits**: Max 10 queued, max 5 active (FIFO).
- **Technical Retries**: At most 1 automatic retry on preflight errors; creative retries require explicit approval.

## 5. Quality & Verification Gates
Before committing any changes, run:
```bash
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
.venv/bin/pyright --pythonpath .venv/bin/python src
.venv/bin/pytest -q tests/auth/test_verification.py tests/api/test_video_request.py tests/api/test_client_launch_kwargs.py tests/api/transports/test_ui_automation_video.py tests/api/transports/test_migrated_composer.py tests/test_flow_batch.py tests/mcp
.venv/bin/gflow doctor
```
All checks must pass with 0 errors/warnings.

