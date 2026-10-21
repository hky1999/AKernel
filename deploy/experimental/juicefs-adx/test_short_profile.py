from pathlib import Path
import unittest

from lab import LabError
from short_profile import render_short_toml, runtime_root, socket_lengths


class ShortProfileTests(unittest.TestCase):
    def test_nodes_have_distinct_bounded_roots(self):
        self.assertNotEqual(runtime_root("source"), runtime_root("target"))
        for node in ("source", "target"):
            lengths = socket_lengths(runtime_root(node))
            self.assertLess(lengths["runsc"]["bytes"], 108)
            self.assertTrue(all(v["bytes"] < 100 for k, v in lengths.items() if k.startswith("fc_")))

    def test_rejects_unknown_node(self):
        with self.assertRaises(LabError):
            runtime_root("../other")

    def test_rejects_actual_native05_long_socket(self):
        original = Path("/mnt/m0-xfs/cn-a0-adx-juicefs-20261015-jPA2cD/native05/source/sandboxd-root")
        with self.assertRaises(LabError):
            socket_lengths(original)

    def test_changes_only_runtime_root_not_shared_filestore(self):
        original = Path("/task/source/sandboxd-root")
        text = 'rootDir = "/task/source/sandboxd-root"\n[runtime]\nfilestore_dir = "/task/mount/runtime/source/filestore"\n'
        rendered = render_short_toml(text, original, runtime_root("source"))
        self.assertIn('rootDir = "/run/a0/s/r"', rendered)
        self.assertIn('filestore_dir = "/task/mount/runtime/source/filestore"', rendered)

    def test_rejects_wrong_or_ambiguous_original(self):
        for text in ('rootDir = "/foreign"\n',
                     'rootDir = "/task/root"\nother = "/task/root"\n'):
            with self.assertRaises(LabError):
                render_short_toml(text, Path("/task/root"), runtime_root("source"))


if __name__ == "__main__":
    unittest.main()
