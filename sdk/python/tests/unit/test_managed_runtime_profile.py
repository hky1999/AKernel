import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from akernel_sdk import DockerRuntime, build_image
from akernel_sdk._managed_build import upload_context
from akernel_sdk.types import CommandResult


class ManagedProfileContract(unittest.TestCase):
    def test_only_capability_name_is_submitted(self):
        options = DockerRuntime(profile="harbor-v1").sandbox_options()
        self.assertEqual(
            options, {"extra_config": {"managedRuntimeProfile": "harbor-v1"}}
        )
        self.assertNotIn("mounts", options)
        for value in ["../node", "http://node", ""]:
            with self.assertRaises(ValueError):
                DockerRuntime(profile=value)

    def test_context_archive_preserves_links_modes_and_empty_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "empty").mkdir()
            (root / "file").write_text("data")
            (root / "file").chmod(0o640)
            (root / "link").symlink_to("file")
            sandbox = MagicMock()

            def inspect(source, _target):
                with tarfile.open(source) as archive:
                    self.assertTrue(archive.getmember("./link").issym())
                    self.assertEqual(archive.getmember("./file").mode, 0o640)
                    self.assertTrue(archive.getmember("./empty").isdir())

            sandbox.files.copy_from_local.side_effect = inspect
            upload_context(sandbox, root, "/work/context")

    def test_image_build_needs_no_client_registry_or_node_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Dockerfile").write_text("FROM scratch\n")
            sandbox = MagicMock()
            sandbox.id = "owned-builder"
            handle = {
                "reference": "private.test/task@sha256:" + "a" * 64,
                "image_process": {
                    "Env": ["MODE=native"],
                    "WorkingDir": "/app",
                    "User": "1000",
                },
            }

            def execute(command, **_kwargs):
                return CommandResult(
                    stdout=json.dumps(handle)
                    if command == "cat /work/artifact.json"
                    else "",
                    stderr="",
                    exit_code=0,
                )

            sandbox.commands.run.side_effect = execute
            with patch("akernel_sdk.sandbox.Sandbox", return_value=sandbox) as factory:
                result = build_image(root, profile="harbor-v1")
            self.assertEqual(
                factory.call_args.kwargs["extra_config"],
                {"managedRuntimeProfile": "harbor-v1"},
            )
            self.assertNotIn("mounts", factory.call_args.kwargs)
            self.assertNotIn("image", factory.call_args.kwargs)
            self.assertEqual(result.envs, {"MODE": "native"})
            self.assertEqual(result.cwd, "/app")
            sandbox.kill.assert_called_once()

    def test_failed_build_retains_primary_error_and_cleanup_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Dockerfile").write_text("FROM scratch\n")
            sandbox = MagicMock()
            sandbox.id = "owned-builder"
            primary = RuntimeError("upstream build failed")

            def execute(command, **_kwargs):
                if "image-build" in command:
                    raise primary
                return CommandResult(stdout="", stderr="", exit_code=0)

            sandbox.commands.run.side_effect = execute
            sandbox.kill.side_effect = RuntimeError("cleanup unavailable")
            with (
                patch("akernel_sdk.sandbox.Sandbox", return_value=sandbox),
                self.assertRaises(RuntimeError) as error,
            ):
                build_image(root, profile="harbor-v1")
            self.assertIs(error.exception, primary)
            self.assertTrue(getattr(error.exception, "cleanup_errors", ()))


if __name__ == "__main__":
    unittest.main()
