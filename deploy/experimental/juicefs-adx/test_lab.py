import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("a0_lab", Path(__file__).with_name("lab.py"))
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)


class LabGuardTests(unittest.TestCase):
    def test_rejects_relative_or_unscoped_roots(self):
        for value in ("relative", "/", "/tmp", "/mnt/m0-xfs/another-task"):
            with self.subTest(value=value), self.assertRaises(lab.LabError):
                lab.checked_root(value)

    def test_rejects_symlink_and_world_writable_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "cn-a0-adx-juicefs-test"
            root.mkdir(mode=0o700)
            self.assertEqual(lab.checked_root(str(root)), root)
            link = Path(tmp) / "cn-a0-adx-juicefs-link"
            link.symlink_to(root, target_is_directory=True)
            with self.assertRaises(lab.LabError):
                lab.checked_root(str(link))
            root.chmod(0o777)
            with self.assertRaises(lab.LabError):
                lab.checked_root(str(root))

    def test_mutation_requires_full_id_and_task_label(self):
        cid = "a" * 64
        info = {"Id": cid, "Config": {"Labels": {lab.LABEL: "mine"}}}
        self.assertTrue(lab.owned_container(info, "mine", cid))
        self.assertFalse(lab.owned_container(info, "other", cid))
        self.assertFalse(lab.owned_container(info, "mine", "a" * 12))
        self.assertFalse(lab.owned_container({"Id": cid}, "mine", cid))

    def test_private_state_replacement_is_mode_600(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            lab.private_json(path, {"phase": 1})
            lab.private_json(path, {"phase": 2})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(lab.json.loads(path.read_text()), {"phase": 2})
            self.assertFalse(path.with_suffix(".json.tmp").exists())


if __name__ == "__main__":
    unittest.main()

