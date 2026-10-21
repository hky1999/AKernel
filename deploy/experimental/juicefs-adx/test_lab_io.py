import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from lab import Lab, LabError, private_json, task_s3_environment


class LabIOTests(unittest.TestCase):
    def test_task_credentials_do_not_inherit_host_role_or_session(self):
        inherited = {"PATH": "/bin", "AWS_ACCESS_KEY_ID": "host-key",
                     "AWS_SECRET_ACCESS_KEY": "host-secret", "AWS_SESSION_TOKEN": "host-token",
                     "AWS_PROFILE": "host-profile", "AWS_WEB_IDENTITY_TOKEN_FILE": "/host/token",
                     "AWS_CONTAINER_CREDENTIALS_FULL_URI": "http://host/credentials"}
        result = task_s3_environment({"access_key": "task-key", "secret_key": "task-secret"}, inherited)
        self.assertEqual(result["AWS_ACCESS_KEY_ID"], "task-key")
        self.assertEqual(result["AWS_SECRET_ACCESS_KEY"], "task-secret")
        self.assertEqual(result["AWS_EC2_METADATA_DISABLED"], "true")
        for name in ("AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_WEB_IDENTITY_TOKEN_FILE",
                     "AWS_CONTAINER_CREDENTIALS_FULL_URI"):
            self.assertNotIn(name, result)
        self.assertEqual(inherited["AWS_SESSION_TOKEN"], "host-token")
        self.assertEqual(result["PATH"], "/bin")

    def test_incomplete_credentials_fail_without_printing_values(self):
        for value in ({}, {"access_key": "private-value"},
                      {"access_key": "private-value", "secret_key": None}):
            with self.assertRaises(LabError) as error:
                task_s3_environment(value, {})
            self.assertNotIn("private-value", str(error.exception))

    def test_timeout_keeps_partial_output_private_and_hides_command(self):
        with tempfile.TemporaryDirectory(prefix="cn-a0-adx-juicefs-test-") as temporary:
            root = Path(temporary)
            (root / "logs").mkdir()
            lab = Lab(root)
            timeout = subprocess.TimeoutExpired(["tool", "private-command-argument"], 1,
                                                output=b"stage reached\n", stderr=b"flush waiting\n")
            with mock.patch("lab.subprocess.run", side_effect=timeout):
                with self.assertRaises(LabError) as error:
                    lab.run(["tool", "private-command-argument"], "timed", timeout=1)
            log = root / "logs/timed.log"
            self.assertEqual(log.read_text(), "stage reached\nflush waiting\n")
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("private-command-argument", str(error.exception))
            self.assertTrue(error.exception.__suppress_context__)

    def test_mount_passes_credentials_to_child_not_command_line(self):
        with tempfile.TemporaryDirectory(prefix="cn-a0-adx-juicefs-test-") as temporary:
            root = Path(temporary)
            for name in ("logs", "secrets", "mount"):
                (root / name).mkdir()
            private_json(root / "secrets/metadata.json", {"url": "postgres://test@127.0.0.1/test"})
            private_json(root / "secrets/s3-user.json", {"keys": [{"access_key": "task-only-access",
                                                                  "secret_key": "task-only-secret"}]})
            lab = Lab(root)
            lab.state = {"task": root.name, "mounts": {}, "juicefs_binary": "/task/bin/juicefs",
                         "data_plane_ready": True}
            child = mock.Mock(pid=123456)
            child.poll.return_value = None
            original_read = Path.read_text

            def read(path, *args, **kwargs):
                if str(path) == "/proc/123456/mountinfo":
                    return "1 0 0:1 / " + str(root / "mount") + " rw - fuse.juicefs task rw\n"
                return original_read(path, *args, **kwargs)

            with mock.patch("lab.subprocess.Popen", return_value=child) as launch, \
                    mock.patch("lab.process_start", return_value="birth"), \
                    mock.patch.object(Path, "read_text", new=read):
                lab.mount("source")
            environment = launch.call_args.kwargs["env"]
            self.assertEqual(environment["AWS_ACCESS_KEY_ID"], "task-only-access")
            self.assertEqual(environment["AWS_SECRET_ACCESS_KEY"], "task-only-secret")
            command = " ".join(launch.call_args.args[0])
            self.assertNotIn("task-only-access", command)
            self.assertNotIn("task-only-secret", command)
            self.assertIs(lab.state["data_plane_ready"], False)
            self.assertIs(lab.state["mounts"]["source"]["ready"], True)

    def test_unready_or_reused_mount_is_not_a_data_probe_target(self):
        with tempfile.TemporaryDirectory(prefix="cn-a0-adx-juicefs-test-") as temporary:
            lab = Lab(Path(temporary))
            lab.state = {"mounts": {"source": {"pid": 7, "start": "old", "ready": True}}}
            with mock.patch("lab.process_start", return_value="new"):
                with self.assertRaises(LabError):
                    lab.namespace_command("source", ["true"])
            lab.state["mounts"]["source"]["ready"] = False
            with mock.patch("lab.process_start", return_value="old"):
                with self.assertRaises(LabError):
                    lab.namespace_command("source", ["true"])


if __name__ == "__main__":
    unittest.main()
