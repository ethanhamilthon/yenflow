# Yenflow

Private, headless Google Flow automation and TOML-driven Image-to-Video (I2V) batch runner.

## Project Defaults

- **Model**: `omni_flash` (Omni 1.1 Flash)
- **Resolution**: `360p` (native Flow resolution)
- **Aspect**: `16:9` (`landscape`)
- **Execution**: `headless = true` (zero window popups, zero focus stealing on macOS/Linux)
- **Credit Safety**: two-phase hash-bound `plan.json` + explicit `approval.json` gate before paid submit

## Directory Structure

```text
yenflow/
├── manifests/          # TOML batch manifests (e.g. batch.example.toml)
├── out/                # Exported media outputs
├── runs/               # Local batch executions (gitignored)
│   └── <run-id>/       # plan.json, approval.json, state.json, report.json, clips/, final/
└── src/gflow_cli/      # Core engine
    ├── flow_batch.py   # Private TOML batch runner, FIFO queue, and validation
    ├── auth/           # Google SSO & Flow session verification
    └── api/            # FlowApiClient & browser transports
```

## Quickstart

### 1. Verify Authentication
```bash
.venv/bin/gflow auth status --profile default
```
Confirms session is active on `flow.google.com` (0 credits spent).

### 2. Run a Batch Manifest
```python
import asyncio
from pathlib import Path
from gflow_cli.paths import default_home, profile_subdir
from gflow_cli.flow_batch import BatchManifest, BatchRunner

async def main():
    manifest = BatchManifest.from_path(Path("manifests/batch.example.toml"))
    profile = profile_subdir(default_home(), "default")
    runner = BatchRunner(manifest, profile_dir=profile, run_dir=Path("runs/batch-01"))

    # Step A: Generates plan.json and computes plan_sha256
    plan_hash = runner.prepare()
    print(f"Plan hash: {plan_hash}")

    # Step B: Require explicit approval before spending credits
    # runner.approval_path.write_text(f'{{"approved": true, "plan_sha256": "{plan_hash}"}}')
    runner.require_approval(plan_hash)

    # Step C: Execute in silent background headless mode
    report = await runner.run()
    print(f"Status: {report['status']}")

asyncio.run(main())
```

### 3. Manifest Example (`manifests/batch.example.toml`)
```toml
[flow]
mode = "i2v"
model = "omni_flash"
aspect = "16:9"
duration = 4
resolution = "360p"
count = 1

[browser]
background = true

[batch]
max_queued = 10
max_active = 5
poll_timeout_s = 600.0
estimated_credits_per_scene = 1

[output]
enhance = false

[[scenes]]
id = "scene-01"
prompt = "First scene prompt description..."
start_frame = "frames/scene-01.png"

[[scenes]]
id = "scene-02"
prompt = "Second scene prompt description..."
start_frame = "frames/scene-02.png"
```

## Invariants & Safety

1. **Credit Lock**: Runner refuses to submit paid jobs unless `approval.json` matches `plan.json` hash.
2. **Reconciliation**: Pre-submit checkpoint (`submit_attempted`) and handle checkpoint (`remote_started`). If browser closes or drops, runner reconciles via status polling without duplicate submit.
3. **Queue Limits**: Maximum 10 queued scenes, maximum 5 active, FIFO. Pause/cancel only affects pending scenes.
4. **Validation**: Every output MP4 is checked via `ffprobe`/`ffmpeg` for exact resolution, duration, and decode integrity.
