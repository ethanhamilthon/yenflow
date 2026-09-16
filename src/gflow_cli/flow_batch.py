"""Private, manifest-driven Flow I2V batch runner.

This module is intentionally not wired into Click or MCP.  It keeps the
credit-bearing boundary small: validate TOML, write a plan, require an
explicit hash-bound approval file, then reuse one headed Flow client while
checkpointing every paid-generation phase locally.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import tomllib
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from gflow_cli.api.video import (
    Aspect,
    GenerateVideoRequest,
    Mode,
    VideoModel,
    VideoResolution,
)
from gflow_cli.errors import BrowserSessionClosedError

__all__ = [
    "BatchApprovalRequired",
    "BatchManifest",
    "BatchManifestError",
    "BatchQueue",
    "BatchRunner",
    "BatchScene",
    "BatchState",
    "CapabilitySnapshot",
    "SceneStatus",
    "run_manifest",
    "stitch_video_files",
    "validate_video_file",
]

_MAX_SCENES = 10
_MAX_ACTIVE = 5
_AUTO_RETRIES = 1
_TARGET_CLOSED_MARKERS = (
    "Target page, context or browser has been closed",
    "Target closed",
)


class BatchManifestError(ValueError):
    """The manifest cannot be safely converted into a paid request."""


class BatchApprovalRequiredError(RuntimeError):
    """The plan has no matching explicit approval file."""


BatchApprovalRequired = BatchApprovalRequiredError


class BatchPersistenceError(RuntimeError):
    """A checkpoint could not be durably written."""


class SceneStatus(StrEnum):
    PENDING = "pending"
    PREFLIGHT = "preflight"
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"
    COMPLETED = "completed"
    FAILED = "failed"
    INDETERMINATE = "indeterminate"
    UNSUPPORTED = "unsupported"
    TIER_LOCKED = "tier_locked"
    CANCELLED = "cancelled"


class CapabilityStatus(StrEnum):
    AVAILABLE = "available"
    UNSUPPORTED = "unsupported"
    TIER_LOCKED = "tier_locked"
    HOST_UNPORTED = "host_unported"
    UNKNOWN = "unknown"


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _write_json_atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        try:
            os.chmod(temp_path, 0o600)
        except OSError:
            pass
        os.replace(temp_path, path)
    except BaseException:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BatchManifestError(f"invalid JSON file: {path}") from exc
    if not isinstance(value, dict):
        raise BatchManifestError(f"JSON file must contain an object: {path}")
    raw_dict = cast(dict[object, object], value)
    return {str(k): v for k, v in raw_dict.items()}


def _table(value: object, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise BatchManifestError(f"{name} must be a TOML table")
    raw_dict = cast(dict[object, object], value)
    return {str(k): v for k, v in raw_dict.items()}


def _reject_unknown(value: Mapping[str, Any], allowed: set[str], name: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise BatchManifestError(f"{name} has unsupported field(s): {', '.join(unknown)}")


def _required_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BatchManifestError(f"{name} must be a non-empty string")
    return value.strip()


def _optional_string(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _required_string(value, name)


def _int_field(value: object, name: str, *, minimum: int, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BatchManifestError(f"{name} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        ceiling = f"-{maximum}" if maximum is not None else "+"
        raise BatchManifestError(f"{name} must be {minimum}{ceiling}")
    return value


def _bool_field(value: object, name: str, *, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise BatchManifestError(f"{name} must be true or false")
    return value


def _resolve_input(path_value: object, name: str, manifest_path: Path) -> Path:
    raw = _required_string(path_value, name)
    try:
        resolved = (manifest_path.parent / raw).resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise BatchManifestError(f"{name} does not exist: {raw}") from exc
    if not resolved.is_file():
        raise BatchManifestError(f"{name} is not a file: {raw}")
    return resolved


@dataclass(frozen=True, slots=True)
class FlowBatchSettings:
    mode: Mode
    model: VideoModel
    aspect: Aspect
    duration: int
    resolution: VideoResolution
    count: int
    project_id: str | None
    project_title: str | None
    poll_timeout_s: float


@dataclass(frozen=True, slots=True)
class BrowserBatchSettings:
    background: bool


@dataclass(frozen=True, slots=True)
class OutputBatchSettings:
    enhance: bool
    audio: Path | None = None


@dataclass(frozen=True, slots=True)
class BatchScene:
    scene_id: str
    prompt: str
    start_frame: Path
    end_frame: Path | None = None

    def request(self, settings: FlowBatchSettings) -> GenerateVideoRequest:
        return GenerateVideoRequest(
            prompt=self.prompt,
            mode=settings.mode,
            model=settings.model,
            aspect=settings.aspect,
            duration=settings.duration,
            resolution=settings.resolution,
            count=settings.count,
            start_image=self.start_frame,
            end_image=self.end_frame,
        )

    def to_plan(self) -> dict[str, Any]:
        return {
            "id": self.scene_id,
            "prompt": self.prompt,
            "start_frame": str(self.start_frame),
            "end_frame": str(self.end_frame) if self.end_frame is not None else None,
        }


@dataclass(frozen=True, slots=True)
class BatchManifest:
    source_path: Path
    manifest_sha256: str
    flow: FlowBatchSettings
    browser: BrowserBatchSettings
    output: OutputBatchSettings
    scenes: tuple[BatchScene, ...]
    max_queued: int = _MAX_SCENES
    max_active: int = _MAX_ACTIVE
    estimated_credits_per_scene: int | None = None

    @classmethod
    def from_path(cls, path: Path) -> BatchManifest:
        try:
            source = path.resolve(strict=True)
            raw = source.read_bytes()
            document = tomllib.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise BatchManifestError(f"could not read TOML manifest: {path}") from exc

        _reject_unknown(document, {"flow", "browser", "batch", "output", "scenes"}, "manifest")
        flow_doc = _table(document.get("flow"), "flow")
        _reject_unknown(
            flow_doc,
            {
                "mode",
                "model",
                "aspect",
                "duration",
                "resolution",
                "count",
                "project_id",
                "project_title",
            },
            "flow",
        )
        mode_value = _required_string(flow_doc.get("mode"), "flow.mode").lower()
        if mode_value != Mode.I2V.value:
            raise BatchManifestError("flow.mode must be 'i2v' in Phase A")
        try:
            model = VideoModel.from_cli(
                _required_string(flow_doc.get("model", "omni_flash"), "flow.model")
            )
            aspect = Aspect.from_cli(_required_string(flow_doc.get("aspect"), "flow.aspect"))
            resolution = VideoResolution.from_cli(
                _required_string(flow_doc.get("resolution", "360p"), "flow.resolution")
            )
        except ValueError as exc:
            raise BatchManifestError(str(exc)) from exc
        if model is None:  # pragma: no cover - model is required above
            raise BatchManifestError("flow.model must be explicit")
        duration = _int_field(flow_doc.get("duration"), "flow.duration", minimum=4, maximum=10)
        count = _int_field(flow_doc.get("count"), "flow.count", minimum=1, maximum=1)
        project_id = _optional_string(flow_doc.get("project_id"), "flow.project_id")
        project_title = _optional_string(flow_doc.get("project_title"), "flow.project_title")

        browser_doc = _table(document.get("browser"), "browser")
        _reject_unknown(browser_doc, {"background"}, "browser")
        background = _bool_field(browser_doc.get("background"), "browser.background", default=True)

        batch_doc = _table(document.get("batch"), "batch")
        _reject_unknown(
            batch_doc,
            {"max_queued", "max_active", "poll_timeout_s", "estimated_credits_per_scene"},
            "batch",
        )
        max_queued = _int_field(
            batch_doc.get("max_queued", _MAX_SCENES),
            "batch.max_queued",
            minimum=1,
            maximum=_MAX_SCENES,
        )
        max_active = _int_field(
            batch_doc.get("max_active", _MAX_ACTIVE),
            "batch.max_active",
            minimum=1,
            maximum=_MAX_ACTIVE,
        )
        poll_timeout_s_value = batch_doc.get("poll_timeout_s", 600.0)
        if isinstance(poll_timeout_s_value, bool) or not isinstance(
            poll_timeout_s_value, (int, float)
        ):
            raise BatchManifestError("batch.poll_timeout_s must be a positive number")
        poll_timeout_s = float(poll_timeout_s_value)
        if poll_timeout_s <= 0:
            raise BatchManifestError("batch.poll_timeout_s must be a positive number")
        estimated = batch_doc.get("estimated_credits_per_scene")
        if estimated is not None:
            estimated = _int_field(
                estimated,
                "batch.estimated_credits_per_scene",
                minimum=0,
            )

        output_doc = _table(document.get("output"), "output")
        _reject_unknown(output_doc, {"enhance", "audio"}, "output")
        audio_val = output_doc.get("audio")
        audio_path = (
            _resolve_input(audio_val, "output.audio", source) if audio_val is not None else None
        )
        output = OutputBatchSettings(
            enhance=_bool_field(output_doc.get("enhance"), "output.enhance", default=False),
            audio=audio_path,
        )

        raw_scenes = document.get("scenes")
        if not isinstance(raw_scenes, list) or not raw_scenes:
            raise BatchManifestError("scenes must be a non-empty TOML array")
        scenes_list = cast(list[object], raw_scenes)
        if len(scenes_list) > max_queued:
            raise BatchManifestError(
                f"scenes has {len(scenes_list)} entries; max_queued is {max_queued}"
            )
        scenes: list[BatchScene] = []
        seen: set[str] = set()
        for index, raw_scene in enumerate(scenes_list, start=1):
            if not isinstance(raw_scene, dict):
                raise BatchManifestError(f"scenes[{index}] must be a TOML table")
            raw_scene_dict = cast(dict[object, object], raw_scene)
            scene_map: dict[str, Any] = {str(k): v for k, v in raw_scene_dict.items()}
            _reject_unknown(
                scene_map,
                {"id", "prompt", "start_frame", "end_frame"},
                f"scenes[{index}]",
            )
            scene_id = _required_string(scene_map.get("id"), f"scenes[{index}].id")
            if scene_id in seen:
                raise BatchManifestError(f"duplicate scene id: {scene_id}")
            if any(char in scene_id for char in "/\\\x00"):
                raise BatchManifestError(f"scene id contains a path separator: {scene_id}")
            seen.add(scene_id)
            prompt = _required_string(scene_map.get("prompt"), f"scenes[{index}].prompt")
            start_frame = _resolve_input(
                scene_map.get("start_frame"),
                f"scenes[{index}].start_frame",
                source,
            )
            end_frame_value = scene_map.get("end_frame")
            end_frame = (
                _resolve_input(end_frame_value, f"scenes[{index}].end_frame", source)
                if end_frame_value is not None
                else None
            )
            scenes.append(
                BatchScene(
                    scene_id=scene_id,
                    prompt=prompt,
                    start_frame=start_frame,
                    end_frame=end_frame,
                )
            )

        # This constructs the exact request shape once per scene and therefore
        # catches duration/model/frame incompatibilities before Chrome launch.
        flow = FlowBatchSettings(
            mode=Mode.I2V,
            model=model,
            aspect=aspect,
            duration=duration,
            resolution=resolution,
            count=count,
            project_id=project_id,
            project_title=project_title,
            poll_timeout_s=poll_timeout_s,
        )
        try:
            for scene in scenes:
                scene.request(flow)
        except (TypeError, ValueError) as exc:
            raise BatchManifestError(str(exc)) from exc
        return cls(
            source_path=source,
            manifest_sha256=hashlib.sha256(raw).hexdigest(),
            flow=flow,
            browser=BrowserBatchSettings(background=background),
            output=output,
            scenes=tuple(scenes),
            max_queued=max_queued,
            max_active=max_active,
            estimated_credits_per_scene=estimated,
        )

    def scene(self, scene_id: str) -> BatchScene:
        for scene in self.scenes:
            if scene.scene_id == scene_id:
                return scene
        raise KeyError(scene_id)

    def to_plan(self) -> dict[str, Any]:
        per_scene = self.estimated_credits_per_scene
        return {
            "schema_version": 1,
            "manifest_sha256": self.manifest_sha256,
            "flow": {
                "mode": self.flow.mode.value,
                "model": self.flow.model.value,
                "aspect": self.flow.aspect.value,
                "duration": self.flow.duration,
                "resolution": self.flow.resolution.value,
                "count": self.flow.count,
                "project_id": self.flow.project_id,
                "project_title": self.flow.project_title,
            },
            "browser": {"background": self.browser.background, "headless": True},
            "limits": {"max_queued": self.max_queued, "max_active": self.max_active},
            "estimate": {
                "scene_count": len(self.scenes),
                "outputs": len(self.scenes) * self.flow.count,
                "credits_per_scene": per_scene,
                "total_credits": per_scene * len(self.scenes) if per_scene is not None else None,
                "enhancement_requested": self.output.enhance,
                "audio": str(self.output.audio) if self.output.audio else None,
            },
            "scenes": [scene.to_plan() for scene in self.scenes],
        }


@dataclass(frozen=True, slots=True)
class CapabilitySnapshot:
    status: CapabilityStatus
    host: str
    controls: dict[str, Any] = field(default_factory=lambda: cast(dict[str, Any], {}))
    missing: tuple[str, ...] = ()
    reason: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> CapabilitySnapshot:
        raw_status = str(value.get("status", CapabilityStatus.UNKNOWN.value))
        try:
            status = CapabilityStatus(raw_status)
        except ValueError:
            status = CapabilityStatus.UNKNOWN
        missing_value = value.get("missing", ())
        missing: tuple[str, ...] = ()
        if isinstance(missing_value, (list, tuple)):
            items = cast(list[object] | tuple[object, ...], missing_value)
            missing = tuple(str(item) for item in items)
        raw_controls = value.get("controls")
        controls: dict[str, Any] = {}
        if isinstance(raw_controls, dict):
            raw_controls_dict = cast(dict[object, object], raw_controls)
            controls = {str(k): v for k, v in raw_controls_dict.items()}
        return cls(
            status=status,
            host=str(value.get("host", "unknown")),
            controls=controls,
            missing=missing,
            reason=str(value["reason"]) if value.get("reason") is not None else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "host": self.host,
            "controls": self.controls,
            "missing": list(self.missing),
            "reason": self.reason,
        }


@dataclass
class SceneState:
    scene_id: str
    status: str = SceneStatus.PENDING.value
    phase: str = "pending"
    attempts: int = 0
    may_have_spent: bool = False
    media_id: str | None = None
    operation_id: str | None = None
    local_path: str | None = None
    metrics: dict[str, Any] = field(default_factory=lambda: cast(dict[str, Any], {}))
    error: str | None = None

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SceneState:
        raw_metrics = value.get("metrics")
        metrics: dict[str, Any] = {}
        if isinstance(raw_metrics, dict):
            raw_metrics_dict = cast(dict[object, object], raw_metrics)
            metrics = {str(k): v for k, v in raw_metrics_dict.items()}
        return cls(
            scene_id=str(value.get("scene_id", "")),
            status=str(value.get("status", SceneStatus.PENDING.value)),
            phase=str(value.get("phase", "pending")),
            attempts=int(value.get("attempts", 0)),
            may_have_spent=bool(value.get("may_have_spent", False)),
            media_id=str(value["media_id"]) if value.get("media_id") else None,
            operation_id=str(value["operation_id"]) if value.get("operation_id") else None,
            local_path=str(value["local_path"]) if value.get("local_path") else None,
            metrics=metrics,
            error=str(value["error"]) if value.get("error") else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "scene_id": self.scene_id,
            "status": self.status,
            "phase": self.phase,
            "attempts": self.attempts,
            "may_have_spent": self.may_have_spent,
            "media_id": self.media_id,
            "operation_id": self.operation_id,
            "local_path": self.local_path,
            "metrics": self.metrics,
            "error": self.error,
        }


@dataclass
class BatchState:
    manifest_sha256: str
    plan_sha256: str
    scenes: dict[str, SceneState]
    project_id: str | None = None
    capability: dict[str, Any] | None = None

    @classmethod
    def new(cls, manifest: BatchManifest, plan_sha256: str) -> BatchState:
        return cls(
            manifest_sha256=manifest.manifest_sha256,
            plan_sha256=plan_sha256,
            scenes={scene.scene_id: SceneState(scene.scene_id) for scene in manifest.scenes},
            project_id=manifest.flow.project_id,
        )

    @classmethod
    def load(cls, path: Path, manifest: BatchManifest, plan_sha256: str) -> BatchState:
        value = _read_json(path)
        if value.get("manifest_sha256") != manifest.manifest_sha256:
            raise BatchManifestError("state belongs to a different manifest")
        if value.get("plan_sha256") != plan_sha256:
            raise BatchManifestError("state belongs to a different plan")
        raw_scenes = value.get("scenes")
        if not isinstance(raw_scenes, dict):
            raise BatchManifestError("state.scenes must be an object")
        scenes_obj_dict = cast(dict[object, object], raw_scenes)
        scenes_dict: dict[str, Any] = {str(k): v for k, v in scenes_obj_dict.items()}
        expected = {scene.scene_id for scene in manifest.scenes}
        if set(scenes_dict) != expected:
            raise BatchManifestError("state scene ids do not match the manifest")
        scenes: dict[str, SceneState] = {}
        for scene_id, raw_value in scenes_dict.items():
            if isinstance(raw_value, dict):
                raw_val_dict = cast(dict[object, object], raw_value)
                scenes[scene_id] = SceneState.from_dict(
                    {str(k): v for k, v in raw_val_dict.items()}
                )
        if len(scenes) != len(expected):
            raise BatchManifestError("state contains malformed scene entries")
        raw_cap = value.get("capability")
        capability: dict[str, Any] | None = None
        if isinstance(raw_cap, dict):
            raw_cap_dict = cast(dict[object, object], raw_cap)
            capability = {str(k): v for k, v in raw_cap_dict.items()}
        return cls(
            manifest_sha256=manifest.manifest_sha256,
            plan_sha256=plan_sha256,
            scenes=scenes,
            project_id=str(value["project_id"]) if value.get("project_id") else None,
            capability=capability,
        )

    def save(self, path: Path) -> None:
        _write_json_atomic(
            path,
            {
                "schema_version": 1,
                "manifest_sha256": self.manifest_sha256,
                "plan_sha256": self.plan_sha256,
                "project_id": self.project_id,
                "capability": self.capability,
                "scenes": {scene_id: scene.to_dict() for scene_id, scene in self.scenes.items()},
            },
        )

    def pending_ids(self) -> list[str]:
        return [
            scene_id
            for scene_id, scene in self.scenes.items()
            if scene.status == SceneStatus.PENDING.value
        ]


class BatchQueue:
    """Pure FIFO queue enforcing the batch's pending and active ceilings."""

    def __init__(self, scene_ids: list[str], *, max_queued: int, max_active: int) -> None:
        if len(scene_ids) > max_queued:
            raise ValueError(f"queue contains {len(scene_ids)} scenes; max queued is {max_queued}")
        if not 1 <= max_active <= _MAX_ACTIVE:
            raise ValueError(f"max_active must be 1-{_MAX_ACTIVE}")
        self._pending = deque(scene_ids)
        self._active: set[str] = set()
        self.max_active = max_active

    def claim_next(self) -> str | None:
        if len(self._active) >= self.max_active or not self._pending:
            return None
        scene_id = self._pending.popleft()
        self._active.add(scene_id)
        return scene_id

    def finish(self, scene_id: str) -> None:
        self._active.remove(scene_id)

    def cancel_pending(self) -> list[str]:
        cancelled = list(self._pending)
        self._pending.clear()
        return cancelled

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def active_count(self) -> int:
        return len(self._active)


def approval_matches(path: Path, plan_sha256: str) -> bool:
    try:
        value = _read_json(path)
    except BatchManifestError:
        return False
    return value.get("approved") is True and value.get("plan_sha256") == plan_sha256


def _is_target_closed(exc: BaseException) -> bool:
    return isinstance(exc, BrowserSessionClosedError) or any(
        marker in str(exc) for marker in _TARGET_CLOSED_MARKERS
    )


def _append_log(path: Path, event: str, **fields: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"event": event, **fields}, ensure_ascii=False) + "\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        # Logging must not replace the checkpoint/error that matters.
        return


def _expected_dimensions(resolution: VideoResolution, aspect: Aspect) -> tuple[int, int]:
    pixels = resolution.pixels
    if aspect is Aspect.LANDSCAPE:
        return pixels * 16 // 9, pixels
    if aspect is Aspect.PORTRAIT:
        return pixels, pixels * 16 // 9
    return pixels, pixels


def validate_video_file(
    path: Path,
    *,
    duration: int,
    resolution: VideoResolution,
    aspect: Aspect,
) -> dict[str, Any]:
    """Decode and verify one native MP4 with the installed ffprobe/ffmpeg."""
    if not path.is_file():
        raise BatchManifestError(f"video output is missing: {path}")
    ffprobe = shutil.which("ffprobe")
    ffmpeg = shutil.which("ffmpeg")
    if ffprobe is None or ffmpeg is None:
        raise BatchManifestError("ffprobe and ffmpeg are required for video validation")
    probe = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height:format=duration",
            "-of",
            "json",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        raise BatchManifestError(f"ffprobe could not read video: {path.name}")
    try:
        parsed = json.loads(probe.stdout)
        stream = parsed["streams"][0]
        format_info = parsed["format"]
        width = int(stream["width"])
        height = int(stream["height"])
        actual_duration = float(format_info["duration"])
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BatchManifestError(
            f"ffprobe returned incomplete video metadata: {path.name}"
        ) from exc
    expected_width, expected_height = _expected_dimensions(resolution, aspect)
    if (width, height) != (expected_width, expected_height):
        raise BatchManifestError(
            f"video dimensions {width}x{height} do not match {expected_width}x{expected_height}"
        )
    if abs(actual_duration - duration) > 0.25:
        raise BatchManifestError(
            f"video duration {actual_duration:.3f}s does not match {duration}s"
        )
    decode = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(path), "-f", "null", "-"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    if decode.returncode != 0:
        raise BatchManifestError(f"ffmpeg could not decode video: {path.name}")
    return {
        "width": width,
        "height": height,
        "duration": actual_duration,
        "bytes": path.stat().st_size,
    }


def stitch_video_files(clips: list[Path], out: Path, *, audio: Path | None = None) -> Path:
    """Re-encode a local concat so mixed timestamps cannot create invalid DTS."""
    if not clips:
        raise ValueError("at least one clip is required")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise BatchManifestError("ffmpeg is required to stitch native clips")
    out.parent.mkdir(parents=True, exist_ok=True)
    if len(clips) == 1 and audio is None:
        shutil.copy2(clips[0], out)
        return out

    fd, list_name = tempfile.mkstemp(prefix="flow-concat-", suffix=".txt")
    list_path = Path(list_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for clip in clips:
                escaped = clip.as_posix().replace("'", "'\\''")
                handle.write(f"file '{escaped}'\n")
        cmd = [
            ffmpeg,
            "-y",
            "-v",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_path),
        ]
        if audio is not None:
            cmd.extend(["-i", str(audio), "-c:a", "aac", "-b:a", "192k", "-shortest"])
        else:
            cmd.extend(["-c:a", "aac"])
        cmd.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(out),
            ]
        )
        subprocess.run(
            cmd,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    finally:
        try:
            list_path.unlink()
        except OSError:
            pass
    return out


class BatchRunner:
    """Run one approved manifest with one persistent Flow client/page."""

    def __init__(
        self,
        manifest: BatchManifest,
        *,
        profile_dir: Path,
        run_dir: Path,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.manifest = manifest
        self.profile_dir = profile_dir
        self.run_dir = run_dir
        self.plan_path = run_dir / "plan.json"
        self.approval_path = run_dir / "approval.json"
        self.state_path = run_dir / "state.json"
        self.capabilities_path = run_dir / "capabilities.json"
        self.report_path = run_dir / "report.json"
        self.logs_path = run_dir / "logs.jsonl"
        self.evidence_dir = run_dir / "evidence"
        self.clips_dir = run_dir / "clips"
        self.final_dir = run_dir / "final"
        self._client_factory = client_factory
        self._state: BatchState | None = None
        self._persistence_failed = False
        self._pause_reason: str | None = None
        self._global_error: str | None = None

    def prepare(self) -> str:
        plan = self.manifest.to_plan()
        plan_sha256 = hashlib.sha256(_canonical_json(plan)).hexdigest()
        plan["plan_sha256"] = plan_sha256
        _write_json_atomic(self.plan_path, plan)
        if self.state_path.exists():
            state = BatchState.load(self.state_path, self.manifest, plan_sha256)
        else:
            state = BatchState.new(self.manifest, plan_sha256)
            state.save(self.state_path)
        self._state = state
        return plan_sha256

    def require_approval(self, plan_sha256: str) -> None:
        if not approval_matches(self.approval_path, plan_sha256):
            raise BatchApprovalRequired(
                f'write {{"approved":true,"plan_sha256":"{plan_sha256}"}} '
                f"to {self.approval_path} before paid submit"
            )

    def _save_state(self) -> None:
        if self._state is None:
            raise RuntimeError("runner is not prepared")
        try:
            self._state.save(self.state_path)
        except OSError as exc:
            self._persistence_failed = True
            raise BatchPersistenceError("could not persist batch state") from exc

    def _checkpoint(self, scene: SceneState, *, phase: str) -> None:
        scene.phase = phase
        if phase == "submit_attempted":
            scene.status = SceneStatus.SUBMITTING.value
            scene.may_have_spent = True
        elif phase == "remote_started":
            scene.status = SceneStatus.SUBMITTED.value
            scene.may_have_spent = True

    def _control(self, control_path: Path | None) -> str:
        if control_path is None or not control_path.exists():
            return "run"
        try:
            raw = control_path.read_text(encoding="utf-8").strip()
        except OSError:
            return "run"
        if not raw:
            return "run"
        if raw.startswith("{"):
            try:
                parsed_val: object = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise BatchManifestError(f"invalid control file: {control_path}") from exc
            if isinstance(parsed_val, dict):
                ctrl_dict = cast(dict[object, object], parsed_val)
                raw = str(ctrl_dict.get("command", ""))
            else:
                raw = ""
        command = raw.lower()
        if command not in {"run", "resume", "pause", "cancel"}:
            raise BatchManifestError(
                f"control command must be run, resume, pause, or cancel: {raw}"
            )
        return command

    def _mark_pending_cancelled(self, queue: BatchQueue) -> None:
        assert self._state is not None
        for scene_id in queue.cancel_pending():
            self._state.scenes[scene_id].status = SceneStatus.CANCELLED.value
            self._state.scenes[scene_id].phase = "cancelled"
        self._save_state()

    async def _probe(
        self,
        client: Any,
        project_id: str,
        request: GenerateVideoRequest,
    ) -> CapabilitySnapshot:
        try:
            probe_fn = cast(Callable[..., Awaitable[Any]], client.probe_video_capabilities)
            raw = await probe_fn(
                request=request,
                project_id=project_id,
                out_dir=self.evidence_dir,
            )
        except Exception as exc:  # noqa: BLE001 - unknown is a safe paid boundary
            raw = {
                "status": CapabilityStatus.UNKNOWN.value,
                "host": "unknown",
                "reason": type(exc).__name__,
            }
            _append_log(self.logs_path, "capability_probe_failed", error=type(exc).__name__)
        snapshot = CapabilitySnapshot.from_mapping(
            cast(Mapping[str, Any], raw) if isinstance(raw, Mapping) else {}
        )
        _write_json_atomic(self.capabilities_path, snapshot.to_dict())
        assert self._state is not None
        self._state.capability = snapshot.to_dict()
        self._save_state()
        return snapshot

    async def _reconcile(self, client: Any, scene: SceneState, project_id: str) -> bool:
        if not scene.media_id:
            return False
        poll = getattr(client, "poll_video_status", None)
        if not callable(poll):
            return False
        try:
            poll_fn = cast(Callable[..., Awaitable[Any]], poll)
            status = await poll_fn(
                scene.media_id,
                project_id=project_id,
                initial_delay_s=0,
                poll_interval=5.0,
                timeout_s=self.manifest.flow.poll_timeout_s,
            )
            if not getattr(status, "succeeded", False):
                return False
            download = getattr(client, "download_video", None)
            if not callable(download):
                return False
            download_fn = cast(Callable[..., Awaitable[Any]], download)
            target = self.clips_dir / f"{scene.scene_id}.mp4"
            await download_fn(scene.media_id, target)
            scene.metrics = validate_video_file(
                target,
                duration=self.manifest.flow.duration,
                resolution=self.manifest.flow.resolution,
                aspect=self.manifest.flow.aspect,
            )
            scene.local_path = str(target)
            scene.status = SceneStatus.COMPLETED.value
            scene.phase = "terminal"
            self._save_state()
            return True
        except Exception as exc:  # noqa: BLE001 - reconciliation never resubmits
            _append_log(
                self.logs_path,
                "reconcile_failed",
                scene_id=scene.scene_id,
                error=type(exc).__name__,
            )
            return False

    async def _run_scene(self, client: Any, scene: BatchScene, project_id: str) -> None:
        assert self._state is not None
        state = self._state.scenes[scene.scene_id]
        request = scene.request(self.manifest.flow)
        for _ in range(_AUTO_RETRIES + 1):
            state.attempts += 1
            state.status = SceneStatus.PREFLIGHT.value
            state.phase = "preflight"
            state.error = None
            self._save_state()
            try:

                def checkpoint(value: Any) -> None:
                    if getattr(value, "phase", None) == "submit_attempted":
                        self._checkpoint(state, phase="submit_attempted")
                        self._save_state()
                    elif getattr(value, "phase", None) == "remote_started":
                        self._checkpoint(state, phase="remote_started")
                        try:
                            self._save_state()
                        except BatchPersistenceError:
                            # The handle is in memory and the job may already be
                            # paid. Do not turn a checkpoint I/O failure into a
                            # blind second submit.
                            _append_log(
                                self.logs_path,
                                "checkpoint_persist_failed",
                                scene_id=scene.scene_id,
                                phase="remote_started",
                            )

                generate_fn = cast(Callable[..., Awaitable[Any]], client.generate_video)
                result = await generate_fn(
                    req=request,
                    project_id=project_id,
                    out_dir=self.clips_dir,
                    poll_timeout_s=self.manifest.flow.poll_timeout_s,
                    download=True,
                    on_checkpoint=checkpoint,
                )
                media_id = getattr(getattr(result, "status", None), "media_id", None)
                if media_id:
                    state.media_id = str(media_id)
                operation_id = getattr(result, "flow_operation_id", None)
                if operation_id:
                    state.operation_id = str(operation_id)
                status = getattr(getattr(result, "status", None), "succeeded", False)
                if not status:
                    state.status = SceneStatus.FAILED.value
                    state.phase = "terminal"
                    state.may_have_spent = True
                    state.error = "flow_generation_failed"
                    self._save_state()
                    return
                local_path = getattr(result, "local_path", None)
                if local_path is None:
                    raise BatchManifestError("Flow reported success without a local video")
                local_path = Path(local_path)
                target = self.clips_dir / f"{scene.scene_id}.mp4"
                if local_path.resolve() != target.resolve():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(local_path, target)
                state.metrics = validate_video_file(
                    target,
                    duration=self.manifest.flow.duration,
                    resolution=self.manifest.flow.resolution,
                    aspect=self.manifest.flow.aspect,
                )
                state.local_path = str(target)
                state.status = SceneStatus.COMPLETED.value
                state.phase = "terminal"
                state.may_have_spent = True
                self._save_state()
                _append_log(self.logs_path, "scene_completed", scene_id=scene.scene_id)
                return
            except Exception as exc:  # noqa: BLE001 - phase decides retry safety
                if _is_target_closed(exc):
                    raise
                if state.phase in {"pending", "preflight"} and state.attempts <= _AUTO_RETRIES:
                    state.status = SceneStatus.PENDING.value
                    state.phase = "pending"
                    _append_log(
                        self.logs_path,
                        "scene_technical_retry",
                        scene_id=scene.scene_id,
                        attempt=state.attempts,
                        error=type(exc).__name__,
                    )
                    self._save_state()
                    continue
                if state.media_id and await self._reconcile(client, state, project_id):
                    return
                state.status = (
                    SceneStatus.INDETERMINATE.value
                    if state.may_have_spent or state.phase in {"submit_attempted", "remote_started"}
                    else SceneStatus.FAILED.value
                )
                state.error = type(exc).__name__
                self._save_state()
                _append_log(
                    self.logs_path,
                    "scene_failed",
                    scene_id=scene.scene_id,
                    status=state.status,
                    error=type(exc).__name__,
                )
                return

    def _mark_inflight_indeterminate(self) -> None:
        if self._state is None:
            return
        changed = False
        for scene in self._state.scenes.values():
            if scene.status in {SceneStatus.SUBMITTING.value, SceneStatus.SUBMITTED.value}:
                scene.status = SceneStatus.INDETERMINATE.value
                scene.error = "browser_session_closed"
                scene.may_have_spent = True
                changed = True
        if changed:
            try:
                self._save_state()
            except BatchPersistenceError:
                pass

    async def _run_client(self, client: Any, *, control_path: Path | None) -> None:
        assert self._state is not None
        pending = self._state.pending_ids()
        if not pending:
            return
        project_id = self._state.project_id
        if not project_id:
            create_proj_fn = cast(Callable[..., Awaitable[Any]], client.create_project)
            project = await create_proj_fn(title=self.manifest.flow.project_title)
            project_id = str(project.project_id)
            self._state.project_id = project_id
            self._save_state()

        first_scene = self.manifest.scene(pending[0])
        snapshot = await self._probe(client, project_id, first_scene.request(self.manifest.flow))
        if snapshot.status is not CapabilityStatus.AVAILABLE:
            replacement = {
                CapabilityStatus.UNSUPPORTED: SceneStatus.UNSUPPORTED,
                CapabilityStatus.TIER_LOCKED: SceneStatus.TIER_LOCKED,
                CapabilityStatus.HOST_UNPORTED: SceneStatus.UNSUPPORTED,
            }.get(snapshot.status)
            if replacement is not None:
                for scene_id in pending:
                    self._state.scenes[scene_id].status = replacement.value
                self._save_state()
            return

        queue = BatchQueue(
            pending,
            max_queued=self.manifest.max_queued,
            max_active=self.manifest.max_active,
        )
        while queue.pending_count:
            command = self._control(control_path)
            if command == "pause":
                self._pause_reason = "control_file"
                return
            if command == "cancel":
                self._mark_pending_cancelled(queue)
                return
            scene_id = queue.claim_next()
            if scene_id is None:
                break
            await self._run_scene(client, self.manifest.scene(scene_id), project_id)
            queue.finish(scene_id)

    def _client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory
        from gflow_cli.api.client import FlowApiClient

        return FlowApiClient

    async def _run_browser(self, *, control_path: Path | None) -> None:
        factory = self._client()
        for restart in range(2):
            try:
                async with factory(
                    profile_dir=self.profile_dir,
                    headless=True,
                    background=self.manifest.browser.background,
                    out_dir=self.evidence_dir,
                ) as client:
                    await self._run_client(client, control_path=control_path)
                return
            except Exception as exc:  # noqa: BLE001 - restart only dead browser sessions
                if not _is_target_closed(exc) or restart:
                    raise
                self._mark_inflight_indeterminate()
                _append_log(self.logs_path, "browser_restarting", attempt=restart + 1)

    def _stitch(self) -> tuple[str | None, str | None]:
        assert self._state is not None
        paths = [
            Path(scene.local_path)
            for scene in self._state.scenes.values()
            if scene.status == SceneStatus.COMPLETED.value and scene.local_path
        ]
        paths = [path for path in paths if path.is_file()]
        if not paths:
            return None, None
        target = self.final_dir / "native-stitched.mp4"
        try:
            self.final_dir.mkdir(parents=True, exist_ok=True)
            stitch_video_files(paths, target, audio=self.manifest.output.audio)
            validate_video_file(
                target,
                duration=self.manifest.flow.duration * len(paths),
                resolution=self.manifest.flow.resolution,
                aspect=self.manifest.flow.aspect,
            )
        except Exception as exc:  # noqa: BLE001 - preserve native clips/report
            return None, type(exc).__name__
        return str(target), None

    def _report(self, *, native_path: str | None, final_error: str | None) -> dict[str, Any]:
        assert self._state is not None
        counts: dict[str, int] = {}
        for scene in self._state.scenes.values():
            counts[scene.status] = counts.get(scene.status, 0) + 1
        completed = counts.get(SceneStatus.COMPLETED.value, 0)
        total = len(self._state.scenes)
        if self._pause_reason:
            status = "paused"
        elif completed == total:
            status = "success"
        elif completed:
            status = "partial_success"
        else:
            status = "failed"
        if final_error and completed:
            status = "partial_success"
        report = {
            "status": status,
            "manifest_sha256": self.manifest.manifest_sha256,
            "plan_sha256": self._state.plan_sha256,
            "project_id": self._state.project_id,
            "capability": self._state.capability,
            "counts": counts,
            "native_stitched_path": native_path,
            "final_error": final_error,
            "enhancement": {
                "requested": self.manifest.output.enhance,
                "status": "unsupported" if self.manifest.output.enhance else "not_requested",
            },
            "scenes": [
                {
                    "id": scene.scene_id,
                    "status": scene.status,
                    "media_id": scene.media_id,
                    "operation_id": scene.operation_id,
                    "local_path": scene.local_path,
                    "metrics": scene.metrics,
                    "error": scene.error,
                }
                for scene in self._state.scenes.values()
            ],
        }
        _write_json_atomic(self.report_path, report)
        return report

    async def run(self, *, control_path: Path | None = None) -> dict[str, Any]:
        plan_sha256 = self.prepare()
        self.require_approval(plan_sha256)
        assert self._state is not None
        command = self._control(control_path)
        if command == "pause":
            self._pause_reason = "control_file"
        elif command == "cancel":
            for scene_id in self._state.pending_ids():
                scene = self._state.scenes[scene_id]
                scene.status = SceneStatus.CANCELLED.value
                scene.phase = "cancelled"
            self._save_state()
        elif self._state.pending_ids():
            try:
                await self._run_browser(control_path=control_path)
            except Exception as exc:  # noqa: BLE001 - return durable partial report
                self._global_error = type(exc).__name__
                self._mark_inflight_indeterminate()
                _append_log(self.logs_path, "batch_failed", error=type(exc).__name__)
        native_path, final_error = self._stitch()
        if self._global_error and final_error is None:
            final_error = self._global_error
        return self._report(native_path=native_path, final_error=final_error)


async def run_manifest(
    manifest_path: Path,
    *,
    profile_dir: Path,
    run_dir: Path | None = None,
    control_path: Path | None = None,
    client_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Convenience entry point for private callers; deliberately no CLI wiring."""
    manifest = BatchManifest.from_path(manifest_path)
    target = run_dir or manifest_path.parent / "runs" / uuid4().hex[:12]
    runner = BatchRunner(
        manifest,
        profile_dir=profile_dir,
        run_dir=target,
        client_factory=client_factory,
    )
    return await runner.run(control_path=control_path)
