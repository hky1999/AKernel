"""A runtime capability cannot select another environment's collection."""

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parent))
from provider_service import RuntimeCollections


class CollectionAuthorization(unittest.TestCase):
    def test_invalid_or_foreign_capability_cannot_prepare_mounts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "records").mkdir()
            identity = "a" * 32
            token = identity + ".private-secret"
            record = root / "records" / (identity + ".json")
            record.write_text(
                json.dumps(
                    {
                        "collection_id": identity,
                        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                    }
                )
            )
            record.chmod(0o600)
            broker = RuntimeCollections(root, "/private/image.sock")
            for auth in [
                "",
                "Basic secret",
                "Bearer ../node.secret",
                "Bearer " + identity + ".wrong",
                "Bearer " + "b" * 32 + ".private-secret",
            ]:
                with self.subTest(auth=auth), self.assertRaises(PermissionError):
                    broker.authorize(auth)
            self.assertEqual(broker.providers, {})
            record.chmod(0o644)
            with self.assertRaises(PermissionError):
                broker.authorize("Bearer " + token)
