<!-- mcp-name: io.github.ethanhamilthon/yenflow -->
# Yenflow

Stateless Model Context Protocol (MCP) server and automation engine for Google Flow (Veo video and Imagen generation).

Designed for AI agents (Claude, Cursor, Pi, Zed, Windsurf) to orchestrate professional video and image generation with zero local database requirements and zero window disruptions.

---

## Key Highlights

- **Stateless MCP Core**: The MCP client maintains the entire workflow state machine (prompts, scene sequence, hashes, approvals, and media IDs). The server requires no local database, migrations, or persistent disk lock-in.
- **Silent Background Execution**: Driven entirely through headless Playwright (`headless=True`, `background=True`). Zero visible Chrome windows, zero user focus stealing on macOS/Linux.
- **Two-Phase Credit Lock**: Paid video generation requires generating a deterministic plan hash (`plan_sha256`) and explicit client approval confirmation before spending credits.
- **Project Defaults**:
  - Model: `omni_flash` (Omni 1.1 Flash)
  - Resolution: `360p` (native Flow resolution)
  - Aspect Ratio: `16:9` (`landscape`)
  - Duration: 4s per scene
- **Multi-Scene I2V Batch & Stitch**: Full toolset to plan scenes, generate clips, poll completion, and stitch into a final MP4 via ffmpeg.

---

## MCP Server Configuration

### Claude Desktop (`claude_desktop_config.json`)
```json
{
  "mcpServers": {
    "yenflow": {
      "command": "uvx",
      "args": ["yenflow", "mcp", "run"]
    }
  }
}
```

### Pi Coding Agent (`~/.pi/agent/mcp.json`)
```json
{
  "mcpServers": {
    "yenflow": {
      "command": "yenflow-mcp"
    }
  }
}
```

### Direct Python / Stdio
```bash
python -m yenflow.mcp
# or
yenflow mcp run
```

### Streamable HTTP (Spec Transport)
```bash
yenflow serve --transport http --port 8000
```

---

## MCP Tools Reference

| Tool | Description | State Handling |
| :--- | :--- | :--- |
| `flow_plan_batch` | Validates scenes, estimates credit cost, and computes deterministic `plan_sha256`. | **Pure/Stateless**: returns plan structure and hash directly to client. |
| `flow_generate_scene` | Generates a single scene end-to-end in silent headless background mode. Returns local MP4 path. | Gated by `approved=True`. Output saved to requested path. |
| `flow_submit_scene` | Submits scene to Flow and returns `media_id` immediately for client-driven tracking. | Gated by `approved=True`. Client tracks `media_id`. |
| `flow_poll_scene` | Checks status of a generation by `media_id` and `project_id`. | **Stateless**: queries Flow directly, returns status to client. |
| `flow_stitch_video` | Stitches multiple clip paths into a final video with optional audio via ffmpeg. | Concatenates files and validates resolution/duration. |
| `gflow_generate_video` | Full Veo video generation (t2v, i2v, r2v) with optional character references. | Standalone video generation. |
| `gflow_generate_image` | Google Imagen text-to-image and image-to-image generation. | Standalone image generation. |
| `gflow_get_credits` | Query current Google Flow credit balance. | Read-only check. |
| `gflow_auth_status` | Verify Google Flow session status. | Read-only probe. |

---

## Stateless Batch Workflow Example

An AI agent orchestrating an Image-to-Video story follows a clean 4-step sequence:

```
[Agent / MCP Client]                                   [Yenflow MCP Server]
       |                                                        |
       |  1. flow_plan_batch(scenes=[...])                      |
       |------------------------------------------------------->|
       |<-------------------------------------------------------|
       |     Returns { plan_sha256, estimated_credits, scenes } |
       |                                                        |
       |  2. Review plan_sha256 & confirm approval              |
       |                                                        |
       |  3. flow_generate_scene(..., approved=True)           |
       |------------------------------------------------------->|
       |<-------------------------------------------------------|
       |     Returns { status: "completed", local_path: ... }   |
       |                                                        |
       |  4. flow_stitch_video(clip_paths=[...])                |
       |------------------------------------------------------->|
       |<-------------------------------------------------------|
       |     Returns { status: "completed", output_path: ... }  |
```

---

## CLI Commands

Yenflow also includes a complete Click CLI:

```bash
# Check Google Flow authentication
yenflow auth status

# Check available credits
yenflow credits

# Run health diagnostics
yenflow doctor

# Start MCP stdio server
yenflow mcp run

# Start MCP HTTP server
yenflow serve --port 8000
```

---

## Quality & Verification Gates

```bash
# Lint & Format
ruff check src tests
ruff format --check src tests

# Static Type Verification
pyright src

# Test Suite
pytest -q tests/auth/test_verification.py tests/test_flow_batch.py tests/mcp
```

## License

MIT License.
