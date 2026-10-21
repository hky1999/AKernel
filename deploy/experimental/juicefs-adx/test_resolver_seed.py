import hashlib
import stat
import tempfile
import unittest
from pathlib import Path

from lab import LabError
from short_profile import copy_private_resolver


class ResolverSeedTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.view = self.root / "run-view"
        self.view.mkdir()
        self.relative = Path("systemd/resolve/stub-resolv.conf")
        self.data = b"nameserver 127.0.0.53\noptions edns0 trust-ad\n"

    def test_read_only_copy_preserves_bytes_and_digest(self):
        result = copy_private_resolver(self.view, self.relative, self.data)
        destination = self.view / self.relative
        self.assertTrue(result["created"])
        self.assertEqual(result["sha256"], hashlib.sha256(self.data).hexdigest())
        self.assertEqual(destination.read_bytes(), self.data)
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o444)

    def test_identical_read_only_copy_is_idempotent(self):
        copy_private_resolver(self.view, self.relative, self.data)
        self.assertFalse(copy_private_resolver(self.view, self.relative, self.data)["created"])

    def test_existing_file_is_not_overwritten(self):
        destination = self.view / "resolv.conf"
        destination.write_bytes(b"original\n")
        destination.chmod(0o444)
        with self.assertRaises(LabError):
            copy_private_resolver(self.view, Path("resolv.conf"), self.data)
        self.assertEqual(destination.read_bytes(), b"original\n")

    def test_symlink_parent_is_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.view / "systemd").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            copy_private_resolver(self.view, self.relative, self.data)
        self.assertEqual(list(outside.iterdir()), [])

    def test_symlink_leaf_is_rejected(self):
        outside = self.root / "outside"
        outside.write_bytes(b"original\n")
        (self.view / "resolv.conf").symlink_to(outside)
        with self.assertRaises(OSError):
            copy_private_resolver(self.view, Path("resolv.conf"), self.data)
        self.assertEqual(outside.read_bytes(), b"original\n")

    def test_absolute_and_traversal_paths_are_rejected(self):
        for relative in (Path("/etc/resolv.conf"), Path("../resolv.conf"), Path(".")):
            with self.subTest(relative=relative), self.assertRaises(LabError):
                copy_private_resolver(self.view, relative, self.data)


if __name__ == "__main__":
    unittest.main()
