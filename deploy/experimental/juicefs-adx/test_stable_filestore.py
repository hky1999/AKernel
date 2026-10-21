from pathlib import Path
import json
import os
import tempfile
import tomllib
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from lab import LabError
from short_profile import activate_filestore, render_stable_toml, runtime_root


class StableFilestoreTests(unittest.TestCase):
    def text(self, node):
        return (f'rootDir = "/task/{node}/sandboxd-root"\n'
                '[plugin.runtime]\n'
                f'filestore_dir = "/task/mount/runtime/native05/{node}/filestore"\n'
                '[plugin.runtime.runsc]\nbackground_restore = true\n')

    def render(self, node, text=None):
        return render_stable_toml(
            text if text is not None else self.text(node),
            Path(f"/task/{node}/sandboxd-root"), runtime_root(node),
            Path(f"/task/mount/runtime/native05/{node}/filestore"),
        )

    def test_nodes_share_runtime_path_but_keep_distinct_control_roots(self):
        source = tomllib.loads(self.render("source"))
        target = tomllib.loads(self.render("target"))
        self.assertEqual(source["plugin"]["runtime"]["filestore_dir"], "/run/a0/filestore")
        self.assertEqual(target["plugin"]["runtime"]["filestore_dir"], "/run/a0/filestore")
        self.assertNotEqual(source["rootDir"], target["rootDir"])
        self.assertTrue(source["plugin"]["runtime"]["runsc"]["background_restore"])

    def test_foreign_filestore_is_rejected(self):
        with self.assertRaises(LabError):
            self.render("source", self.text("source").replace("native05/source/filestore", "foreign/filestore"))

    def test_ambiguous_filestore_literal_is_rejected(self):
        with self.assertRaises(LabError):
            self.render("source", self.text("source") + 'other = "/task/mount/runtime/native05/source/filestore"\n')

    def test_missing_runtime_filestore_is_rejected(self):
        with self.assertRaises(LabError):
            self.render("source", 'rootDir = "/task/source/sandboxd-root"\n')

    def test_keeper_pid_comes_from_ready_not_identity_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ready = root / "ready.json"
            keeper = os.getpid() + 2
            ready.write_text(json.dumps({"pid": keeper}))
            members = root / "cgroup.procs"
            members.write_text(f"{keeper}\n{os.getpid() + 1}\n")
            runtime = SimpleNamespace(
                owner_inside=lambda: {}, validate_inside=lambda owner: None,
                ready_path=ready,
            )
            with patch("short_profile.RuntimeNodes", return_value=runtime), \
                    patch.object(Path, "rglob", return_value=iter([members])):
                with self.assertRaisesRegex(LabError, "stopped task services"):
                    activate_filestore(root, "native05", "source")


if __name__ == "__main__":
    unittest.main()
