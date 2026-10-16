"""检查开发入口的控制器前置条件；只写临时普通文件。"""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "io_entrypoint", Path(__file__).with_name("scheduler-dev-entrypoint.py"))
entrypoint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entrypoint)


class CgroupIOPreparationTest(unittest.TestCase):
    def fixture(self, directory, controllers):
        root = Path(directory) / "group"
        root.mkdir()
        (root / "cgroup.controllers").write_text(controllers)
        (root / "cgroup.procs").write_text("12345\n")
        (root / "cgroup.subtree_control").write_text("preserve\n")
        return root

    def test_missing_io_is_rejected_before_any_service_move(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.fixture(tmp, "cpu memory pids")
            with patch.object(entrypoint, "CGROUP", root), \
                    patch.object(Path, "readlink", return_value=Path("cgroup:[same]")), \
                    self.assertRaisesRegex(RuntimeError, "io"):
                entrypoint.prepare_cgroup(io_accounting=True)
            self.assertFalse((root / "services").exists())
            self.assertEqual((root / "cgroup.procs").read_text(), "12345\n")
            self.assertEqual((root / "cgroup.subtree_control").read_text(), "preserve\n")

    def test_requested_controllers_are_delegated_and_service_reserve_kept(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled), tempfile.TemporaryDirectory() as tmp:
                root = self.fixture(tmp, "cpu memory pids io")
                with patch.object(entrypoint, "CGROUP", root), \
                        patch.object(Path, "readlink", return_value=Path("cgroup:[same]")):
                    entrypoint.prepare_cgroup(io_accounting=enabled)
                expected = {"+cpu", "+memory", "+pids"}
                if enabled:
                    expected.add("+io")
                self.assertEqual(set((root / "cgroup.subtree_control").read_text().split()),
                                 expected)
                self.assertEqual((root / "services/cgroup.procs").read_text(), "12345")
                self.assertEqual((root / "services/memory.max").read_text(), str(4 * 1024**3))
                self.assertFalse((root / "io.max").exists())

    def test_default_does_not_require_io(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.fixture(tmp, "cpu memory pids")
            with patch.object(entrypoint, "CGROUP", root), \
                    patch.object(Path, "readlink", return_value=Path("cgroup:[same]")):
                entrypoint.prepare_cgroup()
            self.assertNotIn("+io", (root / "cgroup.subtree_control").read_text())

    def test_non_boolean_request_is_rejected_before_moving_services(self):
        for value in (None, 1, "true"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                root = self.fixture(tmp, "cpu memory pids io")
                with patch.object(entrypoint, "CGROUP", root), \
                        self.assertRaises(RuntimeError):
                    entrypoint.prepare_cgroup(io_accounting=value)
                self.assertFalse((root / "services").exists())

    def test_foreign_cgroup_namespace_is_rejected_before_moves(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.fixture(tmp, "cpu memory pids io")
            with patch.object(entrypoint, "CGROUP", root), \
                    patch.object(Path, "readlink", side_effect=[Path("cgroup:[a]"), Path("cgroup:[b]")]), \
                    self.assertRaises(RuntimeError):
                entrypoint.prepare_cgroup(io_accounting=True)
            self.assertFalse((root / "services").exists())


if __name__ == "__main__":
    unittest.main()
