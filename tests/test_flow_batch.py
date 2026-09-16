from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from gflow_cli.api.video import (
    Aspect,
    GenerateVideoRequest,
    Mode,
    VideoModel,
    VideoResolution,
)
from gflow_cli.errors import BrowserSessionClosedError
from gflow_cli.flow_batch import (
    BatchApprovalRequired,
    BatchManifest,
    BatchManifestError,
    BatchQueue,
    BatchRunner,
    BatchState,
    SceneStatus,
    approval_matches,
    validate_video_file,
)


def _make_dummy_image(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4"
    )
    return path


def _valid_toml(root: Path, *, scene_count: int = 2, extra: str = "") -> Path:
    frames_dir = root / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "[flow]",
        'mode = "i2v"',
        'model = "veo_3_1_lite"',
        'aspect = "16:9"',
        "duration = 4",
        'resolution = "720p"',
        "count = 1",
        'project_id = "proj-123"',
        "",
        "[browser]",
        "background = true",
        "",
        "[batch]",
        "max_queued = 10",
        "max_active = 5",
        "poll_timeout_s = 300.0",
        "estimated_credits_per_scene = 1",
        "",
    ]
    if "[output]" not in extra:
        lines.extend(["[output]", "enhance = false", ""])
    if extra:
        lines.extend([extra, ""])
    for i in range(1, scene_count + 1):
        img = _make_dummy_image(frames_dir / f"scene_{i}.png")
        rel = img.relative_to(root).as_posix()
        lines.extend(
            [
                "[[scenes]]",
                f'id = "scene-{i}"',
                f'prompt = "Prompt for scene {i}"',
                f'start_frame = "{rel}"',
                "",
            ]
        )
    manifest_path = root / "batch.toml"
    manifest_path.write_text("\n".join(lines), encoding="utf-8")
    return manifest_path


class TestBatchManifest:
    def test_parse_valid_manifest(self, tmp_path: Path) -> None:
        manifest_path = _valid_toml(tmp_path, scene_count=3)
        manifest = BatchManifest.from_path(manifest_path)
        assert manifest.flow.mode is Mode.I2V
        assert manifest.flow.model is VideoModel.VEO_3_1_LITE
        assert manifest.flow.aspect is Aspect.LANDSCAPE
        assert manifest.flow.duration == 4
        assert manifest.flow.resolution is VideoResolution.P720
        assert manifest.flow.count == 1
        assert manifest.browser.background is True
        assert manifest.max_queued == 10
        assert manifest.max_active == 5
        assert len(manifest.scenes) == 3
        assert manifest.scene("scene-2").prompt == "Prompt for scene 2"

    def test_rejects_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(BatchManifestError, match="could not read"):
            BatchManifest.from_path(tmp_path / "non_existent.toml")

    def test_rejects_non_i2v_mode(self, tmp_path: Path) -> None:
        manifest_path = _valid_toml(tmp_path, scene_count=1)
        content = manifest_path.read_text().replace('mode = "i2v"', 'mode = "t2v"')
        manifest_path.write_text(content)
        with pytest.raises(BatchManifestError, match="must be 'i2v'"):
            BatchManifest.from_path(manifest_path)

    def test_rejects_exceeding_max_queued(self, tmp_path: Path) -> None:
        manifest_path = _valid_toml(tmp_path, scene_count=1)
        frames_dir = tmp_path / "frames"
        lines = [manifest_path.read_text()]
        for i in range(2, 12):
            img = _make_dummy_image(frames_dir / f"scene_{i}.png")
            lines.extend(
                [
                    "[[scenes]]",
                    f'id = "scene-{i}"',
                    f'prompt = "Prompt {i}"',
                    f'start_frame = "{img.relative_to(tmp_path).as_posix()}"',
                ]
            )
        manifest_path.write_text("\n".join(lines))
        with pytest.raises(BatchManifestError, match="max_queued is 10"):
            BatchManifest.from_path(manifest_path)

    def test_rejects_invalid_duration_for_veo(self, tmp_path: Path) -> None:
        manifest_path = _valid_toml(tmp_path, scene_count=1)
        content = manifest_path.read_text().replace("duration = 4", "duration = 10")
        manifest_path.write_text(content)
        with pytest.raises(BatchManifestError, match="caps at 8s"):
            BatchManifest.from_path(manifest_path)

    def test_rejects_missing_frame_file(self, tmp_path: Path) -> None:
        manifest_path = _valid_toml(tmp_path, scene_count=1)
        (tmp_path / "frames" / "scene_1.png").unlink()
        with pytest.raises(BatchManifestError, match="does not exist"):
            BatchManifest.from_path(manifest_path)

    def test_rejects_duplicate_scene_id(self, tmp_path: Path) -> None:
        manifest_path = _valid_toml(tmp_path, scene_count=1)
        lines = [
            manifest_path.read_text(),
            "[[scenes]]",
            'id = "scene-1"',
            'prompt = "Duplicate"',
            'start_frame = "frames/scene_1.png"',
        ]
        manifest_path.write_text("\n".join(lines))
        with pytest.raises(BatchManifestError, match="duplicate scene id"):
            BatchManifest.from_path(manifest_path)

    def test_rejects_path_traversal_in_scene_id(self, tmp_path: Path) -> None:
        manifest_path = _valid_toml(tmp_path, scene_count=1)
        content = manifest_path.read_text().replace('id = "scene-1"', 'id = "../escape"')
        manifest_path.write_text(content)
        with pytest.raises(BatchManifestError, match="path separator"):
            BatchManifest.from_path(manifest_path)

    def test_rejects_unknown_section(self, tmp_path: Path) -> None:
        manifest_path = _valid_toml(tmp_path, scene_count=1, extra="[unknown_section]\nfoo = 'bar'")
        with pytest.raises(BatchManifestError, match="unsupported field"):
            BatchManifest.from_path(manifest_path)


class TestBatchQueue:
    def test_fifo_order_and_active_cap(self) -> None:
        queue = BatchQueue(["s1", "s2", "s3", "s4", "s5", "s6"], max_queued=10, max_active=3)
        assert queue.pending_count == 6
        assert queue.active_count == 0

        c1 = queue.claim_next()
        c2 = queue.claim_next()
        c3 = queue.claim_next()
        c4 = queue.claim_next()

        assert [c1, c2, c3] == ["s1", "s2", "s3"]
        assert c4 is None
        assert queue.active_count == 3
        assert queue.pending_count == 3

        queue.finish("s2")
        assert queue.active_count == 2
        c5 = queue.claim_next()
        assert c5 == "s4"
        assert queue.active_count == 3

    def test_cancel_pending_leaves_active(self) -> None:
        queue = BatchQueue(["s1", "s2", "s3"], max_queued=10, max_active=2)
        c1 = queue.claim_next()
        assert c1 == "s1"
        cancelled = queue.cancel_pending()
        assert cancelled == ["s2", "s3"]
        assert queue.pending_count == 0
        assert queue.active_count == 1


class TestBatchStateAndApproval:
    def test_plan_and_state_roundtrip(self, tmp_path: Path) -> None:
        manifest_path = _valid_toml(tmp_path, scene_count=2)
        manifest = BatchManifest.from_path(manifest_path)
        run_dir = tmp_path / "run1"
        runner = BatchRunner(manifest, profile_dir=tmp_path / "profile", run_dir=run_dir)
        plan_sha256 = runner.prepare()

        assert runner.plan_path.exists()
        assert runner.state_path.exists()

        loaded_state = BatchState.load(runner.state_path, manifest, plan_sha256)
        assert loaded_state.manifest_sha256 == manifest.manifest_sha256
        assert loaded_state.plan_sha256 == plan_sha256
        assert set(loaded_state.scenes) == {"scene-1", "scene-2"}
        assert loaded_state.scenes["scene-1"].status == SceneStatus.PENDING.value

    def test_approval_matching(self, tmp_path: Path) -> None:
        manifest_path = _valid_toml(tmp_path, scene_count=1)
        manifest = BatchManifest.from_path(manifest_path)
        run_dir = tmp_path / "run1"
        runner = BatchRunner(manifest, profile_dir=tmp_path / "profile", run_dir=run_dir)
        plan_sha256 = runner.prepare()

        with pytest.raises(BatchApprovalRequired):
            runner.require_approval(plan_sha256)

        runner.approval_path.write_text(json.dumps({"approved": False, "plan_sha256": plan_sha256}))
        assert not approval_matches(runner.approval_path, plan_sha256)

        runner.approval_path.write_text(json.dumps({"approved": True, "plan_sha256": "wrong_hash"}))
        assert not approval_matches(runner.approval_path, plan_sha256)

        runner.approval_path.write_text(json.dumps({"approved": True, "plan_sha256": plan_sha256}))
        assert approval_matches(runner.approval_path, plan_sha256)
        runner.require_approval(plan_sha256)


class FakeFlowClient:
    def __init__(
        self,
        *,
        probe_status: str = "available",
        fail_scene_phase: str | None = None,
        simulate_browser_close: bool = False,
    ) -> None:
        self.probe_status = probe_status
        self.fail_scene_phase = fail_scene_phase
        self.simulate_browser_close = simulate_browser_close
        self.browser_launches = 0
        self.generate_calls = 0

    async def __aenter__(self) -> FakeFlowClient:
        self.browser_launches += 1
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    async def create_project(self, title: str | None = None) -> Any:
        return SimpleNamespace(project_id="proj-auto-1")

    async def probe_video_capabilities(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "status": self.probe_status,
            "host": "migrated",
            "controls": {"labels": ["360p", "720p", "4s", "x1", "16:9"]},
            "missing": [],
        }

    async def generate_video(
        self,
        *,
        req: GenerateVideoRequest,
        project_id: str | None,
        out_dir: Path | None,
        poll_timeout_s: float,
        download: bool,
        on_checkpoint: Any = None,
    ) -> Any:
        self.generate_calls += 1
        if self.simulate_browser_close and self.generate_calls == 1:
            if on_checkpoint:
                on_checkpoint(SimpleNamespace(phase="submit_attempted"))
                on_checkpoint(
                    SimpleNamespace(
                        phase="remote_started",
                        flow_operation_id=None,
                        media_ids=("media-closed-1",),
                    )
                )
            raise BrowserSessionClosedError("Target page, context or browser has been closed")

        if on_checkpoint:
            on_checkpoint(SimpleNamespace(phase="submit_attempted"))
            if self.fail_scene_phase == "submit_attempted":
                raise RuntimeError("lost network before response")
            on_checkpoint(
                SimpleNamespace(
                    phase="remote_started",
                    flow_operation_id=f"op-{self.generate_calls}",
                    media_ids=(f"media-{self.generate_calls}",),
                )
            )

        fake_clip = (out_dir or Path("/tmp")) / f"gen_{self.generate_calls}.mp4"
        fake_clip.parent.mkdir(parents=True, exist_ok=True)
        fake_clip.write_bytes(b"dummy_mp4_bytes")

        return SimpleNamespace(
            status=SimpleNamespace(succeeded=True, media_id=f"media-{self.generate_calls}"),
            flow_operation_id=f"op-{self.generate_calls}",
            local_path=fake_clip,
        )

    async def poll_video_status(self, media_id: str, **kwargs: Any) -> Any:
        return SimpleNamespace(succeeded=True, media_id=media_id)

    async def download_video(self, media_id: str, out_path: Path) -> Path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"dummy_mp4_bytes")
        return out_path


class TestBatchRunnerFlow:
    @pytest.mark.asyncio
    async def test_full_run_with_approval_and_fake_client(self, tmp_path: Path) -> None:
        manifest_path = _valid_toml(tmp_path, scene_count=2)
        manifest = BatchManifest.from_path(manifest_path)
        run_dir = tmp_path / "run_ok"
        client = FakeFlowClient()

        runner = BatchRunner(
            manifest,
            profile_dir=tmp_path / "profile",
            run_dir=run_dir,
            client_factory=lambda **kw: client,
        )
        plan_sha256 = runner.prepare()
        runner.approval_path.write_text(json.dumps({"approved": True, "plan_sha256": plan_sha256}))

        val_mock = {"width": 1280, "height": 720, "duration": 4.0}
        with (
            patch("gflow_cli.flow_batch.validate_video_file", return_value=val_mock),
            patch("gflow_cli.flow_batch.stitch_video_files") as mock_stitch,
        ):
            mock_stitch.side_effect = lambda clips, out, **kw: out.write_bytes(b"stitched") and out
            report = await runner.run()

        assert report["status"] == "success"
        assert report["counts"]["completed"] == 2
        assert runner.capabilities_path.exists()
        assert runner.report_path.exists()
        assert runner.logs_path.exists()
        assert client.generate_calls == 2

    @pytest.mark.asyncio
    async def test_unsupported_capability_skips_submit(self, tmp_path: Path) -> None:
        manifest_path = _valid_toml(tmp_path, scene_count=2)
        manifest = BatchManifest.from_path(manifest_path)
        run_dir = tmp_path / "run_unsupported"
        client = FakeFlowClient(probe_status="unsupported")

        runner = BatchRunner(
            manifest,
            profile_dir=tmp_path / "profile",
            run_dir=run_dir,
            client_factory=lambda **kw: client,
        )
        plan_sha256 = runner.prepare()
        runner.approval_path.write_text(json.dumps({"approved": True, "plan_sha256": plan_sha256}))

        report = await runner.run()
        assert report["status"] == "failed"
        assert report["counts"].get("unsupported") == 2
        assert client.generate_calls == 0

    @pytest.mark.asyncio
    async def test_indeterminate_outcome_does_not_resubmit(self, tmp_path: Path) -> None:
        manifest_path = _valid_toml(tmp_path, scene_count=1)
        manifest = BatchManifest.from_path(manifest_path)
        run_dir = tmp_path / "run_indeterminate"
        client = FakeFlowClient(fail_scene_phase="submit_attempted")

        runner = BatchRunner(
            manifest,
            profile_dir=tmp_path / "profile",
            run_dir=run_dir,
            client_factory=lambda **kw: client,
        )
        plan_sha256 = runner.prepare()
        runner.approval_path.write_text(json.dumps({"approved": True, "plan_sha256": plan_sha256}))

        report = await runner.run()
        assert report["status"] == "failed"
        assert report["counts"].get("indeterminate") == 1
        assert client.generate_calls == 1

    @pytest.mark.asyncio
    async def test_pause_and_cancel_via_control_file(self, tmp_path: Path) -> None:
        manifest_path = _valid_toml(tmp_path, scene_count=2)
        manifest = BatchManifest.from_path(manifest_path)
        run_dir = tmp_path / "run_ctrl"
        client = FakeFlowClient()
        control_path = run_dir / "control.json"
        control_path.parent.mkdir(parents=True, exist_ok=True)
        control_path.write_text(json.dumps({"command": "pause"}))

        runner = BatchRunner(
            manifest,
            profile_dir=tmp_path / "profile",
            run_dir=run_dir,
            client_factory=lambda **kw: client,
        )
        plan_sha256 = runner.prepare()
        runner.approval_path.write_text(json.dumps({"approved": True, "plan_sha256": plan_sha256}))

        report = await runner.run(control_path=control_path)
        assert report["status"] == "paused"
        assert client.generate_calls == 0

        control_path.write_text(json.dumps({"command": "cancel"}))
        report2 = await runner.run(control_path=control_path)
        assert report2["counts"].get("cancelled") == 2

    @pytest.mark.asyncio
    async def test_browser_crash_restarts_once_and_marks_indeterminate(
        self, tmp_path: Path
    ) -> None:
        manifest_path = _valid_toml(tmp_path, scene_count=2)
        manifest = BatchManifest.from_path(manifest_path)
        run_dir = tmp_path / "run_crash"
        client = FakeFlowClient(simulate_browser_close=True)

        runner = BatchRunner(
            manifest,
            profile_dir=tmp_path / "profile",
            run_dir=run_dir,
            client_factory=lambda **kw: client,
        )
        plan_sha256 = runner.prepare()
        runner.approval_path.write_text(json.dumps({"approved": True, "plan_sha256": plan_sha256}))

        val_mock = {"width": 1280, "height": 720, "duration": 4.0}
        with (
            patch("gflow_cli.flow_batch.validate_video_file", return_value=val_mock),
            patch("gflow_cli.flow_batch.stitch_video_files") as mock_stitch,
        ):
            mock_stitch.side_effect = lambda clips, out, **kw: out.write_bytes(b"stitched") and out
            report = await runner.run()

        # The browser restarted once (2 launches total).
        assert client.browser_launches == 2
        # First scene was in-flight when target closed, marked indeterminate.
        # Second scene ran after restart and completed.
        assert report["counts"].get("indeterminate") == 1
        assert report["counts"].get("completed") == 1
        assert report["status"] == "partial_success"


class TestVideoValidationAndStitching:
    def test_validate_video_file_success(self, tmp_path: Path) -> None:
        video_file = tmp_path / "clip.mp4"
        video_file.write_bytes(b"dummy")

        probe_json = json.dumps(
            {
                "streams": [{"width": 1280, "height": 720}],
                "format": {"duration": "4.02"},
            }
        )
        fake_probe = SimpleNamespace(returncode=0, stdout=probe_json, stderr="")
        fake_decode = SimpleNamespace(returncode=0, stdout="", stderr="")

        with (
            patch("shutil.which", return_value="/bin/ffmpeg"),
            patch("subprocess.run", side_effect=[fake_probe, fake_decode]),
        ):
            metrics = validate_video_file(
                video_file,
                duration=4,
                resolution=VideoResolution.P720,
                aspect=Aspect.LANDSCAPE,
            )
        assert metrics["width"] == 1280
        assert metrics["height"] == 720
        assert metrics["duration"] == 4.02

    def test_validate_video_file_dimension_mismatch(self, tmp_path: Path) -> None:
        video_file = tmp_path / "clip.mp4"
        video_file.write_bytes(b"dummy")

        probe_json = json.dumps(
            {
                "streams": [{"width": 640, "height": 360}],
                "format": {"duration": "4.0"},
            }
        )
        fake_probe = SimpleNamespace(returncode=0, stdout=probe_json, stderr="")

        with (
            patch("shutil.which", return_value="/bin/ffmpeg"),
            patch("subprocess.run", return_value=fake_probe),
        ):
            with pytest.raises(BatchManifestError, match="dimensions"):
                validate_video_file(
                    video_file,
                    duration=4,
                    resolution=VideoResolution.P720,
                    aspect=Aspect.LANDSCAPE,
                )

    def test_stitch_video_single_clip_copies(self, tmp_path: Path) -> None:
        from gflow_cli.flow_batch import stitch_video_files

        src = tmp_path / "c1.mp4"
        src.write_bytes(b"clip1")
        dst = tmp_path / "out.mp4"
        res = stitch_video_files([src], dst)
        assert res.exists()
        assert res.read_bytes() == b"clip1"

    def test_stitch_video_multi_clips_calls_ffmpeg(self, tmp_path: Path) -> None:
        from gflow_cli.flow_batch import stitch_video_files

        c1 = tmp_path / "c1.mp4"
        c2 = tmp_path / "c2.mp4"
        c1.write_bytes(b"1")
        c2.write_bytes(b"2")
        dst = tmp_path / "final.mp4"

        with (
            patch("shutil.which", return_value="/bin/ffmpeg"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = SimpleNamespace(returncode=0)
            res = stitch_video_files([c1, c2], dst)
            assert res == dst
            assert mock_run.call_count == 1
            cmd = mock_run.call_args[0][0]
            assert "concat" in cmd
            assert "-c:v" in cmd
            assert "libx264" in cmd

    def test_manifest_accepts_valid_audio_track(self, tmp_path: Path) -> None:
        audio = tmp_path / "soundtrack.mp3"
        audio.write_bytes(b"dummy_mp3")
        manifest_path = _valid_toml(
            tmp_path,
            scene_count=1,
            extra='[output]\nenhance = false\naudio = "soundtrack.mp3"',
        )
        manifest = BatchManifest.from_path(manifest_path)
        assert manifest.output.audio == audio.resolve()
        plan = manifest.to_plan()
        assert plan["estimate"]["audio"] == str(audio.resolve())

    def test_manifest_rejects_missing_audio_file(self, tmp_path: Path) -> None:
        manifest_path = _valid_toml(
            tmp_path,
            scene_count=1,
            extra='[output]\nenhance = false\naudio = "missing.mp3"',
        )
        with pytest.raises(BatchManifestError, match="does not exist"):
            BatchManifest.from_path(manifest_path)

    def test_stitch_video_with_audio_muxes_track(self, tmp_path: Path) -> None:
        from gflow_cli.flow_batch import stitch_video_files

        c1 = tmp_path / "c1.mp4"
        audio = tmp_path / "track.mp3"
        c1.write_bytes(b"1")
        audio.write_bytes(b"sound")
        dst = tmp_path / "final.mp4"

        with (
            patch("shutil.which", return_value="/bin/ffmpeg"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = SimpleNamespace(returncode=0)
            res = stitch_video_files([c1], dst, audio=audio)
            assert res == dst
            assert mock_run.call_count == 1
            cmd = mock_run.call_args[0][0]
            assert "-i" in cmd
            assert str(audio) in cmd
            assert "-shortest" in cmd
