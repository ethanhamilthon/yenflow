# Hidden Flow I2V Batch Runner Implementation Plan

> Internal runner only. No new public CLI command or MCP tool.

**Goal:** Run a repeatable TOML-described I2V batch through one hidden headed Flow context, with bounded queueing, resumable state, no duplicate submits, and compact output.

**Architecture:** Reuse `FlowApiClient` and the existing UI transport. Add a small pure runner module for manifest/state/queue decisions and a `background` launch seam on `FlowApiClient`; do not add a second browser driver, database, or service. The first implementation keeps the existing transport's safe serial generation lock; the queue contract is ready for a future split submit/poll adapter after live evidence.

**Scope:** Phase A only — hidden I2V batch, native local stitch, technical validation, resume/reconcile state, and local plan/approval files. Image adapters, T2V/R2V, Flow Scene/video upscale, and custom Tools follow later.

## File structure

### New files

```text
src/gflow_cli/flow_batch.py
  Pure TOML manifest, plan/state, bounded FIFO runner helpers.
tests/test_flow_batch.py
  Unit tests for validation, queue transitions, checkpoint safety, and reports.
docs/superpowers/plans/2026-09-16-hidden-flow-batch-runner/SCENARIO.md
  Scenario matrix and acceptance risks.
```

### Modified files

```text
src/gflow_cli/api/client.py
  Optional background/minimized headed launch argument; existing default unchanged.
tests/api/test_client_launch_kwargs.py
  Pin background launch behaviour.
docs/ARCHITECTURE.md
  Document the internal hidden-browser runner and its serial transport boundary.
```

## Task 1 — Pure runner contract and tests

**What:** Add the smallest pure manifest, state, queue, approval, and report primitives.

**Steps:**

- [x] Parse TOML with stdlib `tomllib`.
- [x] Resolve frame paths relative to the manifest and reject unsafe/missing inputs.
- [x] Enforce 1–10 scenes, exact model/settings fields, and I2V requirements.
- [x] Persist atomic JSON state before submit and after every checkpoint.
- [x] Represent `pending`, `submitted`, `completed`, `indeterminate`, `failed`, and `unsupported`.
- [x] Keep full state local; expose only compact summaries.

**Tests:**

- [x] Valid manifest resolves 5 local frames.
- [x] Empty, >10, missing-frame, invalid-duration/model manifests fail before browser setup.
- [x] State round-trips a submitted media id and nullable operation id.
- [x] Resume never requeues `submitted`/`indeterminate` scenes.
- [x] FIFO queue enforces 10 queued / 5 active and pending-only cancellation.
- [x] Approval hash rejects a changed plan.

## Task 2 — Background Flow client seam

**What:** Let the private runner launch the existing headed persistent context without foregrounding it.

**Steps:**

- [x] Add `background=False` to `FlowApiClient`; preserve all existing callers.
- [x] Add `--start-minimized` and `--window-position=20000,20000` when `background=True` and headed; default `background=True` to clean `headless=True` on migrated host.
- [x] Keep stealth flags, profile lease, cookie seeding, and one-page setup unchanged.
- [x] Never call `page.bring_to_front` or open a second profile.
- [x] Add a macOS zero-credit focus smoke procedure and record the result locally.

**Tests:**

- [x] Default launch kwargs remain byte-compatible.
- [x] Background mode defaults to headless; headed background kwargs contain `--start-minimized` and `--window-position=20000,20000`.
- [x] Focus restoration on macOS via AppleScript.

## Task 3 — Hidden I2V vertical slice

**What:** Use one `FlowApiClient` context for the batch, run the existing I2V adapter, and persist checkpoints.

**Steps:**

- [x] Build exact `GenerateVideoRequest` values from the manifest.
- [x] Run read-only capability probe before approval/submit.
- [x] Write `plan.json`; wait for matching `approval.json`.
- [x] Submit scenes through the existing safe transport boundary.
- [x] Reconcile lost responses and restart the context once after a browser crash.
- [x] Download and validate every native MP4.
- [x] Assemble native local output with the existing FFmpeg helper.
- [x] Return `partial_success` when optional enhancement is unavailable.

**Tests:**

- [x] Fake client exercises checkpoints and no-resubmit recovery.
- [x] Browser exception restarts once and reconciles handles.
- [x] Native artifacts remain after optional upscale failure.
- [x] Output validation rejects wrong duration/resolution/decode failure.

## Task 4 — Documentation and gates

**Steps:**

- [x] Document the private runner contract and background limitation.
- [x] State explicitly that no CLI/MCP surface changed.
- [x] Run focused unit tests, ruff, format, and pyright.
- [x] Run the zero-credit browser smoke for background focus and capability probe.
- [ ] Run live I2V only after the generated plan is explicitly approved.

## Definition of done

- [x] Phase A tasks complete.
- [x] Focus remains on the user's existing app during background launch.
- [x] No duplicate submit after unknown post-submit outcome.
- [x] Native artifacts and report survive a missing high-res feature.
- [x] Focused quality gates pass.
- [x] Live verification result or named external blocker is recorded.
