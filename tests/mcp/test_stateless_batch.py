# SPDX-License-Identifier: MIT
"""Unit tests for stateless MCP batch runner tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gflow_cli.api.video import VideoStatus
from gflow_cli.mcp.server import _SPEND_TOOLS, server
from gflow_cli.mcp.stateless_batch import (
    compute_plan_hash,
    flow_generate_scene,
    flow_plan_batch,
    flow_poll_scene,
    flow_stitch_video,
    flow_submit_scene,
    yenflow_plan_batch,
)


@pytest.mark.asyncio
async def test_stateless_tools_registered_on_server() -> None:
    tools = await server.list_tools()
    tool_names = {t.name for t in tools}
    expected = {
        "flow_plan_batch",
        "yenflow_plan_batch",
        "flow_submit_scene",
        "yenflow_submit_scene",
        "flow_generate_scene",
        "yenflow_generate_scene",
        "flow_poll_scene",
        "yenflow_poll_scene",
        "flow_stitch_video",
        "yenflow_stitch_video",
    }
    for name in expected:
        assert name in tool_names, f"Tool {name!r} not found in MCP server registry"


def test_spend_tools_contain_paid_batch_endpoints() -> None:
    assert "flow_generate_scene" in _SPEND_TOOLS
    assert "flow_submit_scene" in _SPEND_TOOLS
    assert "yenflow_generate_scene" in _SPEND_TOOLS
    assert "yenflow_submit_scene" in _SPEND_TOOLS


@pytest.mark.asyncio
async def test_plan_batch_valid_scenes(tmp_path: Path) -> None:
    img1 = tmp_path / "frame1.png"
    img2 = tmp_path / "frame2.png"
    img1.write_bytes(b"image1")
    img2.write_bytes(b"image2")

    scenes: list[dict[str, Any]] = [
        {"prompt": "A fox runs across snow", "start_frame": str(img1)},
        {"prompt": "The fox leaps into air", "start_frame": str(img2), "duration": 4},
    ]

    res = await flow_plan_batch(scenes=scenes, model="omni_flash", aspect="16:9", duration=4)
    assert res["valid"] is True
    assert res["total_scenes"] == 2
    assert res["estimated_credits"] == 2
    assert isinstance(res["plan_sha256"], str)
    assert len(res["plan_sha256"]) == 64
    assert res["settings"]["model"] == "omni_flash"
    assert res["settings"]["aspect"] == "landscape"
    assert res["settings"]["duration"] == 4
    assert len(res["scenes"]) == 2

    # Deterministic hash check
    res2 = await yenflow_plan_batch(scenes=scenes, model="omni_flash", aspect="16:9", duration=4)
    assert res["plan_sha256"] == res2["plan_sha256"]


@pytest.mark.asyncio
async def test_plan_batch_validation_failures() -> None:
    # Empty scenes
    res = await flow_plan_batch(scenes=[])
    assert res["valid"] is False
    assert "non-empty" in res["error"]

    # Missing prompt
    res = await flow_plan_batch(scenes=[{"start_frame": "frame.png"}])
    assert res["valid"] is False
    assert "prompt" in res["error"]

    # Missing start_frame
    res = await flow_plan_batch(scenes=[{"prompt": "hello"}])
    assert res["valid"] is False
    assert "start_frame" in res["error"]

    # Exceeding max scenes (>10)
    scenes = [{"prompt": f"scene {i}", "start_frame": f"f{i}.png"} for i in range(11)]
    res = await flow_plan_batch(scenes=scenes)
    assert res["valid"] is False
    assert "maximum allowed batch size" in res["error"]

    # Invalid duration
    res = await flow_plan_batch(
        scenes=[{"prompt": "p", "start_frame": "f.png"}],
        duration=15,
    )
    assert res["valid"] is False
    assert "duration" in res["error"]

    # Invalid aspect
    res = await flow_plan_batch(
        scenes=[{"prompt": "p", "start_frame": "f.png"}],
        aspect="21:9",
    )
    assert res["valid"] is False
    assert "Invalid setting" in res["error"]


@pytest.mark.asyncio
async def test_submit_scene_two_phase_credit_lock() -> None:
    # Attempting to submit without approval must be blocked immediately
    res = await flow_submit_scene(
        prompt="A car drives fast",
        start_frame="any.png",
        approved=False,
    )
    assert res["status"] == "error"
    assert "approval required" in res["error"]


@pytest.mark.asyncio
async def test_submit_scene_missing_file(tmp_path: Path) -> None:
    non_existent = str(tmp_path / "missing.png")
    res = await flow_submit_scene(
        prompt="A car drives fast",
        start_frame=non_existent,
        approved=True,
    )
    assert res["status"] == "error"
    assert "does not exist" in res["error"]


@pytest.mark.asyncio
async def test_generate_scene_two_phase_credit_lock() -> None:
    res = await flow_generate_scene(
        prompt="A car drives fast",
        start_frame="any.png",
        approved=False,
    )
    assert res["status"] == "error"
    assert "approval required" in res["error"]


@pytest.mark.asyncio
async def test_poll_scene(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_status = VideoStatus(
        media_id="media-123",
        status="MEDIA_GENERATION_STATUS_SUCCESSFUL",
        failure_reasons=(),
    )

    mock_client = MagicMock()
    mock_client.check_video_status = AsyncMock(return_value=mock_status)

    class MockClientContext:
        async def __aenter__(self) -> MagicMock:
            return mock_client

        async def __aexit__(self, *args: Any) -> None:
            pass

    with patch("gflow_cli.mcp.stateless_batch.FlowApiClient", return_value=MockClientContext()):
        res = await flow_poll_scene(media_id="media-123", project_id="proj-456")
        assert res["media_id"] == "media-123"
        assert res["project_id"] == "proj-456"
        assert res["completed"] is True
        assert res["failed"] is False
        assert res["status"] == "MEDIA_GENERATION_STATUS_SUCCESSFUL"


@pytest.mark.asyncio
async def test_stitch_video(tmp_path: Path) -> None:
    clip1 = tmp_path / "clip1.mp4"
    clip2 = tmp_path / "clip2.mp4"
    clip1.write_bytes(b"mp4-1")
    clip2.write_bytes(b"mp4-2")
    out_target = tmp_path / "stitched.mp4"

    with patch("gflow_cli.mcp.stateless_batch.stitch_video_files") as mock_stitch:
        res = await flow_stitch_video(
            clip_paths=[str(clip1), str(clip2)],
            output_path=str(out_target),
        )
        assert res["status"] == "completed"
        assert res["total_clips"] == 2
        assert res["output_path"] == str(out_target)
        mock_stitch.assert_called_once()

    # Empty clip list
    res_err = await flow_stitch_video(clip_paths=[], output_path=str(out_target))
    assert res_err["status"] == "error"

    # Non-existent clip
    res_missing = await flow_stitch_video(
        clip_paths=[str(tmp_path / "non_existent.mp4")],
        output_path=str(out_target),
    )
    assert res_missing["status"] == "error"
    assert "does not exist" in res_missing["error"]


def test_canonical_hash_reproducibility() -> None:
    p1 = {"b": 2, "a": 1}
    p2 = {"a": 1, "b": 2}
    assert compute_plan_hash(p1) == compute_plan_hash(p2)
