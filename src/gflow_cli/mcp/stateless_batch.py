# SPDX-License-Identifier: MIT
"""Stateless MCP batch runner tools for Google Flow I2V.

These tools enable any MCP client (Claude, Cursor, Pi, etc.) to orchestrate
multi-scene I2V batch pipelines statelessly. The MCP client maintains the
state machine (scene list, approval hashes, media IDs, progress). The server
maintains no local database or disk-bound job state.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import structlog

from gflow_cli.api.client import FlowApiClient
from gflow_cli.api.video import (
    Aspect,
    GenerateVideoRequest,
    Mode,
    VideoModel,
    VideoResolution,
    VideoStarted,
)
from gflow_cli.flow_batch import stitch_video_files, validate_video_file
from gflow_cli.mcp.server import server
from gflow_cli.mcp.tools import guarded
from gflow_cli.paths import default_home, profile_subdir

log = structlog.get_logger()

_DEFAULT_PROFILE = "default"
_MAX_SCENES = 10


def _canonical_json(value: object) -> bytes:
    """Return deterministic, sorted UTF-8 JSON bytes."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def compute_plan_hash(plan_dict: dict[str, Any]) -> str:
    """Compute deterministic SHA-256 hash of a batch plan."""
    return hashlib.sha256(_canonical_json(plan_dict)).hexdigest()


# ---------------------------------------------------------------------------
# Tool: flow_plan_batch
# ---------------------------------------------------------------------------


async def _plan_batch_impl(
    scenes: list[dict[str, Any]],
    model: str = "omni_flash",
    aspect: str = "16:9",
    duration: int = 4,
    resolution: str = "360p",
    count: int = 1,
    background: bool = True,
    estimated_credits_per_scene: int = 1,
) -> dict[str, Any]:
    """Validate scenes and compute deterministic plan hash without mutating server state."""
    if not scenes:
        return {
            "valid": False,
            "error": "scenes must be a non-empty list of scene specifications.",
        }

    if len(scenes) > _MAX_SCENES:
        return {
            "valid": False,
            "error": f"scenes list exceeds maximum allowed batch size ({_MAX_SCENES}).",
        }

    try:
        validated_model = VideoModel.from_cli(model)
        validated_aspect = Aspect.from_cli(aspect)
        validated_resolution = VideoResolution.from_cli(resolution)
    except ValueError as exc:
        return {"valid": False, "error": f"Invalid setting: {exc}"}

    if validated_model is None:
        return {"valid": False, "error": f"Unknown model: {model}"}

    if duration < 4 or duration > 10:
        return {"valid": False, "error": "duration must be between 4 and 10 seconds."}

    normalized_scenes: list[dict[str, Any]] = []
    for idx, raw_scene in enumerate(scenes, start=1):
        prompt = raw_scene.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return {"valid": False, "error": f"Scene #{idx} missing required non-empty 'prompt'."}

        start_frame = raw_scene.get("start_frame")
        if not isinstance(start_frame, str) or not start_frame.strip():
            return {
                "valid": False,
                "error": f"Scene #{idx} missing required non-empty 'start_frame'.",
            }

        end_frame = raw_scene.get("end_frame")
        if end_frame is not None and (not isinstance(end_frame, str) or not end_frame.strip()):
            end_frame = None

        scene_id = str(raw_scene.get("scene_id") or raw_scene.get("id") or f"scene_{idx:02d}")

        normalized_scenes.append(
            {
                "id": scene_id,
                "prompt": prompt.strip(),
                "start_frame": start_frame.strip(),
                "end_frame": end_frame.strip() if end_frame else None,
            }
        )

    plan_payload = {
        "flow": {
            "mode": "i2v",
            "model": validated_model.value,
            "aspect": validated_aspect.value,
            "duration": duration,
            "resolution": validated_resolution.value,
            "count": count,
        },
        "browser": {
            "background": background,
        },
        "scenes": normalized_scenes,
    }

    plan_sha256 = compute_plan_hash(plan_payload)
    estimated_credits = len(normalized_scenes) * estimated_credits_per_scene

    return {
        "valid": True,
        "plan_sha256": plan_sha256,
        "estimated_credits": estimated_credits,
        "total_scenes": len(normalized_scenes),
        "settings": {
            "model": validated_model.value,
            "aspect": validated_aspect.value,
            "duration": duration,
            "resolution": validated_resolution.value,
            "count": count,
            "background": background,
        },
        "scenes": normalized_scenes,
    }


@server.tool(
    name="flow_plan_batch",
    description=(
        "Plan an I2V video batch statelessly. Validates scenes and parameters, "
        "computes credit cost and deterministic SHA-256 plan hash, and returns "
        "the plan state to the MCP client. Zero server-side persistence."
    ),
)
@guarded
async def flow_plan_batch(
    scenes: list[dict[str, Any]],
    model: str = "omni_flash",
    aspect: str = "16:9",
    duration: int = 4,
    resolution: str = "360p",
    count: int = 1,
    background: bool = True,
    estimated_credits_per_scene: int = 1,
) -> dict[str, Any]:
    return await _plan_batch_impl(
        scenes=scenes,
        model=model,
        aspect=aspect,
        duration=duration,
        resolution=resolution,
        count=count,
        background=background,
        estimated_credits_per_scene=estimated_credits_per_scene,
    )


@server.tool(
    name="yenflow_plan_batch",
    description="Alias for flow_plan_batch.",
)
@guarded
async def yenflow_plan_batch(
    scenes: list[dict[str, Any]],
    model: str = "omni_flash",
    aspect: str = "16:9",
    duration: int = 4,
    resolution: str = "360p",
    count: int = 1,
    background: bool = True,
    estimated_credits_per_scene: int = 1,
) -> dict[str, Any]:
    return await flow_plan_batch(
        scenes=scenes,
        model=model,
        aspect=aspect,
        duration=duration,
        resolution=resolution,
        count=count,
        background=background,
        estimated_credits_per_scene=estimated_credits_per_scene,
    )


# ---------------------------------------------------------------------------
# Tool: flow_submit_scene
# ---------------------------------------------------------------------------


async def _submit_scene_impl(
    prompt: str,
    start_frame: str,
    end_frame: str | None = None,
    model: str = "omni_flash",
    aspect: str = "16:9",
    duration: int = 4,
    resolution: str = "360p",
    project_id: str | None = None,
    plan_sha256: str | None = None,
    approved: bool = True,
    profile: str = _DEFAULT_PROFILE,
) -> dict[str, Any]:
    """Submit a single scene to Flow in silent headless background mode."""
    if not approved:
        return {
            "status": "error",
            "error": "Two-phase credit lock: approval required before spending credits.",
        }

    start_path = Path(start_frame).resolve()
    if not start_path.is_file():
        return {
            "status": "error",
            "error": f"start_frame file does not exist: {start_frame}",
        }

    end_path = Path(end_frame).resolve() if end_frame else None
    if end_path and not end_path.is_file():
        return {
            "status": "error",
            "error": f"end_frame file does not exist: {end_frame}",
        }

    try:
        video_model = VideoModel.from_cli(model)
        video_aspect = Aspect.from_cli(aspect)
        video_resolution = VideoResolution.from_cli(resolution)
    except ValueError as exc:
        return {"status": "error", "error": str(exc)}

    if video_model is None:
        return {"status": "error", "error": f"Unknown video model: {model}"}

    req = GenerateVideoRequest(
        prompt=prompt,
        mode=Mode.I2V,
        model=video_model,
        aspect=video_aspect,
        duration=duration,
        resolution=video_resolution,
        count=1,
        start_image=start_path,
        end_image=end_path,
    )

    pdir = profile_subdir(default_home(), profile)
    started_event = asyncio.Event()
    discovered_media: list[str] = []

    def on_started(started: VideoStarted) -> None:
        discovered_media.append(started.media_id)
        started_event.set()

    try:
        async with FlowApiClient(profile_dir=pdir, headless=True, background=True) as client:
            resolved_project_id = project_id
            if not resolved_project_id:
                proj = await client.create_project(title="Yenflow Batch")
                resolved_project_id = str(proj.project_id)

            gen_task = asyncio.create_task(
                client.generate_video(
                    req=req,
                    project_id=resolved_project_id,
                    download=False,
                    on_started=on_started,
                )
            )

            try:
                await asyncio.wait_for(started_event.wait(), timeout=90.0)
                media_id = discovered_media[0] if discovered_media else None
                return {
                    "status": "submitted",
                    "media_id": media_id,
                    "project_id": resolved_project_id,
                    "prompt": prompt,
                    "plan_sha256": plan_sha256,
                }
            except TimeoutError:
                res = await asyncio.wait_for(gen_task, timeout=10.0)
                return {
                    "status": "submitted",
                    "media_id": res.status.media_id,
                    "project_id": resolved_project_id,
                    "prompt": prompt,
                    "plan_sha256": plan_sha256,
                }
    except Exception as exc:
        log.warning("flow_submit_scene.failed", error=str(exc))
        return {
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
        }


@server.tool(
    name="flow_submit_scene",
    description=(
        "Submit a single video scene to Google Flow in silent background headless "
        "mode (zero window popups, no stolen focus). Returns media_id immediately "
        "to the client for tracking."
    ),
)
@guarded
async def flow_submit_scene(
    prompt: str,
    start_frame: str,
    end_frame: str | None = None,
    model: str = "omni_flash",
    aspect: str = "16:9",
    duration: int = 4,
    resolution: str = "360p",
    project_id: str | None = None,
    plan_sha256: str | None = None,
    approved: bool = True,
    profile: str = _DEFAULT_PROFILE,
) -> dict[str, Any]:
    return await _submit_scene_impl(
        prompt=prompt,
        start_frame=start_frame,
        end_frame=end_frame,
        model=model,
        aspect=aspect,
        duration=duration,
        resolution=resolution,
        project_id=project_id,
        plan_sha256=plan_sha256,
        approved=approved,
        profile=profile,
    )


@server.tool(
    name="yenflow_submit_scene",
    description="Alias for flow_submit_scene.",
)
@guarded
async def yenflow_submit_scene(
    prompt: str,
    start_frame: str,
    end_frame: str | None = None,
    model: str = "omni_flash",
    aspect: str = "16:9",
    duration: int = 4,
    resolution: str = "360p",
    project_id: str | None = None,
    plan_sha256: str | None = None,
    approved: bool = True,
    profile: str = _DEFAULT_PROFILE,
) -> dict[str, Any]:
    return await flow_submit_scene(
        prompt=prompt,
        start_frame=start_frame,
        end_frame=end_frame,
        model=model,
        aspect=aspect,
        duration=duration,
        resolution=resolution,
        project_id=project_id,
        plan_sha256=plan_sha256,
        approved=approved,
        profile=profile,
    )


# ---------------------------------------------------------------------------
# Tool: flow_generate_scene
# ---------------------------------------------------------------------------


async def _generate_scene_impl(
    prompt: str,
    start_frame: str,
    end_frame: str | None = None,
    model: str = "omni_flash",
    aspect: str = "16:9",
    duration: int = 4,
    resolution: str = "360p",
    output_path: str | None = None,
    project_id: str | None = None,
    plan_sha256: str | None = None,
    approved: bool = True,
    profile: str = _DEFAULT_PROFILE,
    timeout_s: float = 600.0,
) -> dict[str, Any]:
    """Generate a single video scene end-to-end in silent background mode."""
    if not approved:
        return {
            "status": "error",
            "error": "Two-phase credit lock: approval required before spending credits.",
        }

    start_path = Path(start_frame).resolve()
    if not start_path.is_file():
        return {
            "status": "error",
            "error": f"start_frame file does not exist: {start_frame}",
        }

    end_path = Path(end_frame).resolve() if end_frame else None
    if end_path and not end_path.is_file():
        return {
            "status": "error",
            "error": f"end_frame file does not exist: {end_frame}",
        }

    try:
        video_model = VideoModel.from_cli(model)
        video_aspect = Aspect.from_cli(aspect)
        video_resolution = VideoResolution.from_cli(resolution)
    except ValueError as exc:
        return {"status": "error", "error": str(exc)}

    if video_model is None:
        return {"status": "error", "error": f"Unknown video model: {model}"}

    req = GenerateVideoRequest(
        prompt=prompt,
        mode=Mode.I2V,
        model=video_model,
        aspect=video_aspect,
        duration=duration,
        resolution=video_resolution,
        count=1,
        start_image=start_path,
        end_image=end_path,
    )

    pdir = profile_subdir(default_home(), profile)
    out_target = Path(output_path).resolve() if output_path else None
    out_dir = out_target.parent if out_target else None

    try:
        async with FlowApiClient(
            profile_dir=pdir,
            headless=True,
            background=True,
            out_dir=out_dir,
        ) as client:
            resolved_project_id = project_id
            if not resolved_project_id:
                proj = await client.create_project(title="Yenflow Scene")
                resolved_project_id = str(proj.project_id)

            res = await client.generate_video(
                req=req,
                project_id=resolved_project_id,
                out_dir=out_dir,
                poll_timeout_s=timeout_s,
                download=True,
            )

            final_path = res.local_path
            if out_target and final_path and Path(final_path).resolve() != out_target:
                out_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(final_path, out_target)
                final_path = out_target

            metrics = None
            if final_path and Path(final_path).is_file():
                try:
                    metrics = validate_video_file(
                        Path(final_path),
                        duration=duration,
                        resolution=video_resolution,
                        aspect=video_aspect,
                    )
                except Exception:
                    pass

            return {
                "status": "completed",
                "media_id": res.status.media_id,
                "local_path": str(final_path) if final_path else None,
                "project_id": resolved_project_id,
                "duration": duration,
                "aspect": video_aspect.value,
                "resolution": video_resolution.value,
                "metrics": metrics,
                "plan_sha256": plan_sha256,
            }
    except Exception as exc:
        log.warning("flow_generate_scene.failed", error=str(exc))
        return {
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
        }


@server.tool(
    name="flow_generate_scene",
    description=(
        "Generate a single video scene end-to-end in silent headless background "
        "mode. Returns the local path to the generated MP4 file upon completion."
    ),
)
@guarded
async def flow_generate_scene(
    prompt: str,
    start_frame: str,
    end_frame: str | None = None,
    model: str = "omni_flash",
    aspect: str = "16:9",
    duration: int = 4,
    resolution: str = "360p",
    output_path: str | None = None,
    project_id: str | None = None,
    plan_sha256: str | None = None,
    approved: bool = True,
    profile: str = _DEFAULT_PROFILE,
    timeout_s: float = 600.0,
) -> dict[str, Any]:
    return await _generate_scene_impl(
        prompt=prompt,
        start_frame=start_frame,
        end_frame=end_frame,
        model=model,
        aspect=aspect,
        duration=duration,
        resolution=resolution,
        output_path=output_path,
        project_id=project_id,
        plan_sha256=plan_sha256,
        approved=approved,
        profile=profile,
        timeout_s=timeout_s,
    )


@server.tool(
    name="yenflow_generate_scene",
    description="Alias for flow_generate_scene.",
)
@guarded
async def yenflow_generate_scene(
    prompt: str,
    start_frame: str,
    end_frame: str | None = None,
    model: str = "omni_flash",
    aspect: str = "16:9",
    duration: int = 4,
    resolution: str = "360p",
    output_path: str | None = None,
    project_id: str | None = None,
    plan_sha256: str | None = None,
    approved: bool = True,
    profile: str = _DEFAULT_PROFILE,
    timeout_s: float = 600.0,
) -> dict[str, Any]:
    return await flow_generate_scene(
        prompt=prompt,
        start_frame=start_frame,
        end_frame=end_frame,
        model=model,
        aspect=aspect,
        duration=duration,
        resolution=resolution,
        output_path=output_path,
        project_id=project_id,
        plan_sha256=plan_sha256,
        approved=approved,
        profile=profile,
        timeout_s=timeout_s,
    )


# ---------------------------------------------------------------------------
# Tool: flow_poll_scene
# ---------------------------------------------------------------------------


async def _poll_scene_impl(
    media_id: str,
    project_id: str,
    profile: str = _DEFAULT_PROFILE,
) -> dict[str, Any]:
    """Statelessly poll Flow API for the current status of media_id."""
    pdir = profile_subdir(default_home(), profile)
    try:
        async with FlowApiClient(profile_dir=pdir, headless=True, background=True) as client:
            status_obj = await client.check_video_status(media_id, project_id=project_id)
            is_failed = status_obj.status == "MEDIA_GENERATION_STATUS_FAILED"
            return {
                "media_id": media_id,
                "project_id": project_id,
                "status": status_obj.status,
                "failure_reasons": list(status_obj.failure_reasons),
                "error_message": status_obj.error_message,
                "completed": status_obj.succeeded,
                "failed": is_failed,
            }
    except Exception as exc:
        log.warning("flow_poll_scene.failed", error=str(exc))
        return {
            "status": "error",
            "media_id": media_id,
            "error": f"{type(exc).__name__}: {exc}",
        }


@server.tool(
    name="flow_poll_scene",
    description=(
        "Statelessly check Google Flow for the completion status of a video "
        "generation by media ID and project ID."
    ),
)
@guarded
async def flow_poll_scene(
    media_id: str,
    project_id: str,
    profile: str = _DEFAULT_PROFILE,
) -> dict[str, Any]:
    return await _poll_scene_impl(media_id=media_id, project_id=project_id, profile=profile)


@server.tool(
    name="yenflow_poll_scene",
    description="Alias for flow_poll_scene.",
)
@guarded
async def yenflow_poll_scene(
    media_id: str,
    project_id: str,
    profile: str = _DEFAULT_PROFILE,
) -> dict[str, Any]:
    return await flow_poll_scene(media_id=media_id, project_id=project_id, profile=profile)


# ---------------------------------------------------------------------------
# Tool: flow_stitch_video
# ---------------------------------------------------------------------------


@server.tool(
    name="flow_stitch_video",
    description=(
        "Stitch multiple MP4 video clip files together into a single unified "
        "video with optional audio track using ffmpeg."
    ),
)
@guarded
async def flow_stitch_video(
    clip_paths: list[str],
    output_path: str,
    audio_path: str | None = None,
) -> dict[str, Any]:
    if len(clip_paths) < 1:
        return {"status": "error", "error": "clip_paths must contain at least 1 video file path."}

    resolved_paths: list[Path] = []
    for path_str in clip_paths:
        p = Path(path_str).resolve()
        if not p.is_file():
            return {"status": "error", "error": f"Clip file does not exist: {path_str}"}
        resolved_paths.append(p)

    out_file = Path(output_path).resolve()
    out_file.parent.mkdir(parents=True, exist_ok=True)

    audio_file = Path(audio_path).resolve() if audio_path else None
    if audio_file and not audio_file.is_file():
        return {"status": "error", "error": f"Audio file does not exist: {audio_path}"}

    try:
        stitch_video_files(resolved_paths, out_file, audio=audio_file)
        return {
            "status": "completed",
            "output_path": str(out_file),
            "total_clips": len(resolved_paths),
        }
    except Exception as exc:
        log.warning("flow_stitch_video.failed", error=str(exc))
        return {
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
        }


@server.tool(
    name="yenflow_stitch_video",
    description="Alias for flow_stitch_video.",
)
@guarded
async def yenflow_stitch_video(
    clip_paths: list[str],
    output_path: str,
    audio_path: str | None = None,
) -> dict[str, Any]:
    return await flow_stitch_video(
        clip_paths=clip_paths,
        output_path=output_path,
        audio_path=audio_path,
    )
