# Scenario: Hidden Flow I2V batch runner

## Coverage map

Active: D1 auth/session, D2 WAF/reCAPTCHA, D3 UI/background lifecycle, D4 manifest/resume, D5 queue/concurrency, D6 SQLite/state, D7 errors, D8 paths, D9 transport handles, D10 headed/headless, D11 manifest validation, D12 compact observability, and D13 MCP boundary review.

D13 has no implementation surface: this is a private Python runner, not a CLI command or MCP tool. It must not add a user-facing option without a matching MCP decision.

## Scenario table

| # | Dimension | Scenario | Severity | Expected behaviour | Test category |
|---|---|---|---|---|---|
| 1 | D1 | Profile is not Flow-authenticated | Critical | Fail before paid submit with the existing typed auth error; preserve plan/state | Integration |
| 2 | D2 | Five jobs are scheduled with paced submits | High | Never burst beyond the configured active window; one page and one profile are reused | Unit + E2E live |
| 3 | D2 | Submit response is lost after the click | Critical | Mark the scene indeterminate/reconcile; never submit it again automatically | Unit |
| 4 | D3 | Background launch is requested | High | Launch remains headed, adds the minimized/background argument, and never calls `bring_to_front` | Unit + macOS smoke |
| 5 | D3 | Flow UI changes host/cohort during probe | High | Snapshot records the observed host and capability status; unsupported is not treated as available | Integration |
| 6 | D4 | Runner is interrupted after submit | Critical | Resume loads the saved media/operation handles and does not duplicate the scene | Unit + E2E |
| 7 | D4 | Completed output exists but manifest hash changed | High | Mark stale and require an explicit rerun; do not overwrite silently | Unit |
| 8 | D5 | Queue contains 10 scenes | High | FIFO order is preserved; no more than 5 are in flight and pending cancel does not touch submitted work | Unit |
| 9 | D5 | Chrome context dies during polling | Critical | Restart the context once, re-probe, reconcile known handles, then stop with partial success if needed | Integration |
| 10 | D6 | State write fails after a remote start | Critical | Log the persistence failure, keep the generation result, and never convert it into a blind retry | Unit |
| 11 | D7 | Unsupported/tier-locked feature is requested | High | Record explicit status and skip only dependent steps; return partial success with artifacts | Unit |
| 12 | D8 | Manifest/reference path has spaces or Unicode | High | Resolve relative to manifest, preserve platform separators, and upload the exact file | Unit |
| 13 | D9 | Flow returns a media id but no operation id | High | Media id remains the canonical reconciliation handle; operation id is nullable | Unit |
| 14 | D10 | Headless is accidentally enabled | High | Runner forces headed mode for the production adapter; do not silently claim headless support | Unit |
| 15 | D11 | Manifest has 0 or >10 scenes, missing frame, invalid model/duration | High | Reject before browser launch and before any credits can be spent | Unit |
| 16 | D12 | Normal batch produces verbose browser logs | Medium | stdout contains compact progress only; full redacted evidence stays under the run directory | Unit |
| 17 | D13 | A future CLI/MCP option is added for this runner | High | Stop and add the MCP parity decision/payload coverage before exposing it | Review gate |

## Must-cover before merge

1. Persist-before-submit state and no duplicate submit after an indeterminate outcome.
2. Queue limit 10 / active limit 5 and FIFO pending cancellation.
3. Background headed launch does not opt into headless mode or foreground navigation.
4. Invalid manifest/reference paths fail before browser launch.
5. Browser restart reconciles existing handles and never resubmits unknown jobs.
6. Partial success preserves native artifacts when an enhancement/capability step is unavailable.

## Deferred

1. True concurrent submit/poll on one Flow page: current transport serializes a complete video call under its generation lock. The first slice exposes the queue contract and preserves the safe serial transport; a split submit/poll adapter needs a separate live spike.
2. Automatic creative retry and LLM vision review: native batch outputs and compact evidence are the first deliverable; creative changes remain explicit.
3. Flow video-upscale transport and all custom Tools: add only after a read-only capability capture proves the live surface.

## Suggested BDD scenarios

```gherkin
Feature: hidden Flow I2V batch
  Scenario: reject an invalid batch before opening Chrome
    Given a manifest with eleven scenes
    When the private runner validates the manifest
    Then it rejects the plan without creating a browser context

  Scenario: resume a submitted scene without duplicate billing
    Given a scene checkpoint marked remote_started with a media id
    When the runner resumes the batch
    Then it reconciles the media id and does not submit that scene again

  Scenario: pause leaves submitted jobs alone
    Given a batch with pending and submitted scenes
    When the control file requests pause
    Then pending scenes remain pending and submitted scenes continue polling
```

## Known-issues cross-reference

- `KNOWN_ISSUES.md`: migrated Flow coverage is partial; capability detection must use the observed UI and host, never host membership as a capability claim.
- `KNOWN_ISSUES.md`: Playwright headed mode is required for the production UI path; hidden headed is an OS/window-management feature, not a headless bypass.
- `KNOWN_ISSUES.md`: duration controls vary by cohort; exact settings must be checked before submit.
- `KNOWN_ISSUES.md`: profile lease contention and macOS cookie seeding are existing client protections to reuse.
