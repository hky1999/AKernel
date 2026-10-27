"""Lifetime and user metadata stay independent of scheduling and create budgets."""

import unittest
from unittest.mock import MagicMock, patch

import httpx

from akernel_sdk import Sandbox, SandboxQuery, UnsupportedBackendFeatureError
from akernel_sdk._addresses import Endpoint
from akernel_sdk._backends.adx import AdxBackend, _OwnedSandbox, _Session
from akernel_sdk._backends.base import BackendConfig
from akernel_sdk.types import SandboxInfo


class LifetimeMetadataTest(unittest.TestCase):
    def setUp(self):
        self.backend = MagicMock()
        self.session = MagicMock(id="source")
        self.session.get_info.return_value = SandboxInfo(
            "source", "running", 1, 1, "image"
        )
        self.backend.create.return_value = self.session
        self.backend.from_snapshot.return_value = self.session
        patcher = patch("akernel_sdk.sandbox.load_backend", return_value=self.backend)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_public_info_preserves_metadata_and_utc_deadline(self):
        self.session.get_info.return_value = SandboxInfo(
            "source",
            "running",
            1,
            1,
            "image",
            metadata={"task": "test"},
            expires_at_unix_seconds=1800000000,
        )
        with Sandbox() as source:
            info = source.get_info()
            self.assertEqual(info.metadata, {"task": "test"})
            self.assertEqual(info.end_at.timestamp(), 1800000000)

    def test_creation_metadata_is_copied_and_timeouts_are_independent(self):
        metadata = {"task": "first"}
        with Sandbox.create(
            image="image",
            metadata=metadata,
            lifetime_timeout=120,
            timeout=150,
            idle_timeout=0,
        ) as source:
            metadata["task"] = "changed"
            spec = self.backend.create.call_args.args[0]
            self.assertEqual(dict(spec.metadata), {"task": "first"})
            self.assertEqual(
                (spec.create_timeout, spec.lifetime_timeout, spec.idle_timeout),
                (150, 120, 0),
            )
            source.set_timeout(300)
            self.session.set_timeout.assert_called_once_with(300)
        with self.assertRaises(RuntimeError):
            source.set_timeout(300)

    def test_both_snapshot_factories_keep_metadata_and_lifetime(self):
        for factory in (Sandbox.create, Sandbox.from_snapshot):
            with factory("snapshot", metadata={"task": "restore"}, lifetime_timeout=20):
                spec = self.backend.from_snapshot.call_args.args[0]
                self.assertEqual(dict(spec.metadata), {"task": "restore"})
                self.assertEqual(spec.lifetime_timeout, 20)
                self.assertEqual(spec.timeout, 90)

    def test_invalid_inputs_do_not_create_or_restore(self):
        for options in (
            {"metadata": {"a": 1}},
            {"metadata": {"": "value"}},
            {"metadata": {"key": "x" * 4097}},
            {"lifetime_timeout": 0},
            {"lifetime_timeout": True},
        ):
            for identity in (None, "snapshot"):
                with (
                    self.subTest(options=options, identity=identity),
                    self.assertRaises((TypeError, ValueError)),
                ):
                    Sandbox.create(identity, **options)
        self.backend.create.assert_not_called()
        self.backend.from_snapshot.assert_not_called()

    def test_capability_gate_precedes_create_and_preserves_original_body(self):
        native = _OwnedSandbox.__new__(_OwnedSandbox)
        native._sdk_create_fields = {
            "metadata": {"task": "test"},
            "lifetimeTimeoutSeconds": 60,
        }
        native._client = MagicMock()
        response = native._client._http.get.return_value
        response.status_code = 200
        response.json.return_value = {"capabilities": []}
        with patch(
            "adx_environment.Environment._create",
            return_value={"environmentId": "created"},
        ) as create:
            with self.assertRaises(UnsupportedBackendFeatureError):
                native._create({"image": "image"})
            create.assert_not_called()
            response.json.return_value = {
                "capabilities": ["environment-metadata-v1", "environment-lifetime-v1"]
            }
            body = {"image": "image"}
            native._create(body)
            self.assertEqual(body, {"image": "image"})
            self.assertEqual(
                create.call_args.args[0], {**body, **native._sdk_create_fields}
            )

    def test_metadata_filter_fills_pages_without_dropping_continuation(self):
        backend = AdxBackend(
            BackendConfig(
                Endpoint("api", 443, "https", True),
                Endpoint("gateway", 443, "https", True),
                "test",
                True,
            )
        )
        calls = []
        pages = [
            {
                "items": [
                    {"id": "other", "status": "running", "metadata": {"task": "other"}}
                ],
                "nextPageToken": "one",
            },
            {
                "items": [
                    {"id": "mine", "status": "paused", "metadata": {"task": "test"}}
                ],
                "nextPageToken": "two",
            },
            {
                "items": [
                    {"id": "last", "status": "running", "metadata": {"task": "test"}}
                ]
            },
        ]

        def respond(_client, request, **_kwargs):
            calls.append(request)
            body = (
                {"capabilities": ["environment-metadata-v1"]}
                if request.url.path.endswith("capabilities")
                else pages.pop(0)
            )
            return httpx.Response(200, json=body, request=request)

        with patch.object(httpx.Client, "send", autospec=True, side_effect=respond):
            rows, token = backend.list_sandboxes(
                SandboxQuery(metadata={"task": "test"}), None, 1
            )
            self.assertEqual([r.id for r in rows], ["mine"])
            self.assertEqual(token, "two")
            rows, token = backend.list_sandboxes(
                SandboxQuery(metadata={"task": "test"}), token, 1
            )
            self.assertEqual([r.id for r in rows], ["last"])
            self.assertIsNone(token)
        self.assertTrue(all(r.method == "GET" for r in calls))

    def test_set_timeout_updates_duration_only_after_confirmation(self):
        with Sandbox(metadata={"task": "source"}) as _source:
            spec = self.backend.create.call_args.args[0]
        native = MagicMock(id="source")
        native._client._safe_id.return_value = "source"
        native._client._http.get.return_value.status_code = 200
        native._client._http.get.return_value.json.return_value = {
            "capabilities": ["environment-lifetime-v1"]
        }
        with (
            patch("akernel_sdk._backends.adx.adx_environment.Pty"),
            patch("akernel_sdk._backends.adx._control_pty_connection"),
        ):
            session = _Session(native, spec, object())
        native._client._lifecycle_request.return_value = {"success": True}
        session.set_timeout(80)
        self.assertEqual(session._spec.lifetime_timeout, 80)
        native._client._lifecycle_request.assert_called_once_with(
            "source",
            operation="timeout",
            body={"lifetimeTimeoutSeconds": 80},
            timeout_seconds=60,
        )
        native._client._lifecycle_request.side_effect = RuntimeError("unknown outcome")
        with self.assertRaises(RuntimeError):
            session.set_timeout(100)
        self.assertEqual(session._spec.lifetime_timeout, 80)


if __name__ == "__main__":
    unittest.main()
