from pathlib import Path
import shutil
import tempfile
import unittest

from lab import LabError
from private_pki import ensure_pki


class PrivatePkiTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("openssl"), "openssl required")
    def test_creates_verified_private_material_and_reuses_without_rotation(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "pki"
            paths = ensure_pki(directory, "a0-test")
            original = {name: path.read_bytes() for name, path in paths.items()}
            self.assertEqual(directory.stat().st_mode & 0o077, 0)
            self.assertTrue(all(path.stat().st_mode & 0o077 == 0 for path in paths.values()))
            again = ensure_pki(directory, "a0-test")
            self.assertEqual(original, {name: path.read_bytes() for name, path in again.items()})

    def test_incomplete_previous_attempt_is_preserved_and_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "pki"
            directory.mkdir(mode=0o700)
            with self.assertRaises(LabError):
                ensure_pki(directory, "a0-test")
            self.assertTrue(directory.exists())

    def test_refuses_symlink_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            target = base / "real"
            target.mkdir(mode=0o700)
            link = base / "pki"
            link.symlink_to(target, target_is_directory=True)
            with self.assertRaises(LabError):
                ensure_pki(link, "a0-test")
