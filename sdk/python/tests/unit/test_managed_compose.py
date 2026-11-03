"""Managed projects use runtime operations; restore never repeats startup."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from akernel_sdk import ComposeProject, DockerRuntime, HttpReverseTunnel
from akernel_sdk.types import CommandResult


class ManagedCompose(unittest.TestCase):
    def test_managed_project_delegates_build_and_register_and_restores_handles(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = Path(temporary) / "context"
            context.mkdir()
            definition = context / "docker-compose.yaml"
            definition.write_text(
                "services: {main: {build: .}, api: {image: ubuntu:24.04}}\n"
            )
            project = ComposeProject.prepare(
                context,
                [definition],
                network="bridge",
                runtime=DockerRuntime(profile="harbor-v1"),
            )
            self.assertEqual(
                project.sandbox_options()["extra_config"],
                {"managedRuntimeProfile": "harbor-v1"},
            )
            self.assertEqual(project.sandbox_options()["mounts"], [])
            sandbox = MagicMock()
            sandbox.commands.run.return_value = CommandResult(
                stdout="", stderr="", exit_code=0
            )
            with (
                patch.object(DockerRuntime, "start"),
                patch("akernel_sdk._managed_build.upload_context") as upload,
            ):
                project.start_runtime(sandbox)
            upload.assert_called_once()
            project.build()
            command = sandbox.commands.run.call_args.args[0]
            self.assertIn("/usr/lib/akernel/compose.py build", command)
            self.assertNotIn("--no-cache", command)
            attached = project.descriptor
            attached["files"].append("/harbor/environment.build/runtime-images.yaml")
            sandbox.commands.run.return_value = CommandResult(
                stdout=json.dumps(attached), stderr="", exit_code=0
            )
            project.pull()
            restored = MagicMock()
            captured = ComposeProject.attach(restored, project.descriptor)
            with self.assertRaises(RuntimeError):
                captured.start_runtime(restored)
            captured.reconnect_network()
            self.assertEqual(restored.commands.run.call_count, 1)
            self.assertEqual(
                restored.commands.run.call_args.args[0],
                "python3 /usr/lib/akernel/compose.py reconnect",
            )

    def test_restored_project_never_creates_replacement_tunnel(self):
        descriptor = {
            "schema_version": 1,
            "project_name": "test",
            "services": ["main"],
            "directory": "/project",
            "files": [],
            "managed_runtime_profile": "harbor-v1",
        }
        sandbox = MagicMock()
        project = ComposeProject.attach(sandbox, descriptor)
        with (
            patch(
                "akernel_sdk._compose.ServiceHandle.identity",
                return_value={"container_id": "same", "started_at": "same"},
            ),
            self.assertRaises(RuntimeError),
        ):
            project.service("main").expose_tunnel(
                HttpReverseTunnel("http://127.0.0.1:12345")
            )
        sandbox.commands.run.assert_not_called()
