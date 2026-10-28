# Copyright (c) 2026 Ant Group Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from unittest.mock import MagicMock, patch

import httpx

from akernel_sdk import (
    Client,
    NetworkPolicy,
    Sandbox,
    SnapshotInfo,
    Template,
    TemplateInfo,
    _templates,
    cli,
)
from akernel_sdk import sandbox as public
from akernel_sdk._addresses import Endpoint
from akernel_sdk._backends import adx
from akernel_sdk._backends.base import BackendConfig, SnapshotRestoreSpec
from akernel_sdk._backends.errors import (
    BackendOperationError,
    UnsupportedBackendFeatureError,
)


class TemplateTests(unittest.TestCase):
    def setUp(self):
        self.backend = MagicMock()
        self.patch = patch.object(_templates, "load_backend", return_value=self.backend)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_import_get_delete_use_distinct_template_identity(self):
        info = TemplateInfo("tpl-a", "snapshot", "python")
        self.backend.import_template.return_value = info
        result = Template.import_snapshot(SnapshotInfo("snapshot"), name="python")
        self.assertEqual(result, info)
        args = self.backend.import_template.call_args.args
        self.assertEqual(args[:2], ("snapshot", "python"))
        self.assertTrue(args[2].startswith("tpl-"))
        self.backend.get_template.return_value = info
        self.assertEqual(Template.get(info), info)
        self.backend.get_template.assert_called_once_with("tpl-a")
        Template.delete(info)
        self.backend.delete_template.assert_called_once_with("tpl-a")
        self.backend.delete_snapshot.assert_not_called()

    def test_listing_is_lazy_and_preserves_cursor_on_failure(self):
        info = TemplateInfo("tpl-a", "snapshot", "python")
        self.backend.list_templates.side_effect = [
            ([info], "next"),
            RuntimeError("offline"),
            ([], None),
        ]
        pages = Template.list(name="python", limit=1)
        self.backend.list_templates.assert_not_called()
        self.assertEqual(pages.next_items(), [info])
        with self.assertRaises(RuntimeError):
            pages.next_items()
        self.assertEqual(pages.next_token, "next")
        self.assertEqual(pages.next_items(), [])
        self.assertFalse(pages.has_next)

    def test_invalid_inputs_never_mutate_catalog(self):
        for value in ("", " ", "x/y", "x?query", "x\n"):
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                Template.get(value)
        for limit in (0, True, 1001):
            with self.subTest(limit=limit), self.assertRaises((ValueError, TypeError)):
                Template.list(limit=limit)
        self.backend.get_template.assert_not_called()

    def test_client_catalog_does_not_modify_global_configuration(self):
        with patch("akernel_sdk._client.new_backend", return_value=self.backend):
            client = Client("https://cluster.example", "test-only")
        info = TemplateInfo("tpl-a", "s", "python")
        self.backend.get_template.return_value = info
        self.assertEqual(client.Template.get("tpl-a"), info)


class TemplateCLITests(unittest.TestCase):
    def run_cli(self, args):
        output, errors = io.StringIO(), io.StringIO()
        with (
            patch.object(sys, "argv", ["ak", "template", *args]),
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            with self.assertRaises(SystemExit) as raised:
                cli.main()
        return raised.exception.code, output.getvalue(), errors.getvalue()

    def test_cli_list_returns_one_page_and_resume_token(self):
        page = MagicMock()
        page.next_items.return_value = [TemplateInfo("tpl-a", "snapshot", "python")]
        page.next_token = "cursor"
        with patch.object(Template, "list", return_value=page) as listing:
            code, output, errors = self.run_cli(["list", "--limit", "1"])
        self.assertEqual(code, 0, errors)
        self.assertEqual(json.loads(output)["next_token"], "cursor")
        self.assertEqual(json.loads(output)["items"][0]["template_id"], "tpl-a")
        listing.assert_called_once_with(name=None, limit=1, next_token=None)

    def test_cli_start_disconnects_without_killing_remote_sandbox(self):
        sandbox = MagicMock()
        sandbox.id = "child"
        with patch.object(Sandbox, "create", return_value=sandbox) as create:
            code, output, errors = self.run_cli(["start", "tpl-a"])
        self.assertEqual(code, 0, errors)
        self.assertEqual(output.strip(), "child")
        create.assert_called_once_with(
            template="tpl-a", idle_timeout=300, timeout=None, detached=True
        )
        sandbox.close.assert_called_once()
        sandbox.kill.assert_not_called()

    def test_cli_errors_are_visible_and_nonzero(self):
        with patch.object(
            Template, "delete", side_effect=BackendOperationError("unavailable")
        ):
            code, output, errors = self.run_cli(["delete", "tpl-a"])
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("unavailable", errors)


class TemplateWireTests(unittest.TestCase):
    def setUp(self):
        self.backend = adx.AdxBackend(
            BackendConfig(
                Endpoint("template-api.example", 443, "https", True),
                Endpoint("template-api.example", 80, "http", False),
                "test-only",
                True,
            )
        )

    def test_template_fork_does_not_guess_an_unreported_effective_policy(self):
        native = MagicMock()
        native.id = "child"
        native.newly_created = True
        spec = SnapshotRestoreSpec(
            snapshot_id="tpl-a",
            template_id="tpl-a",
            idle_timeout=300,
            schedule_timeout=30,
            timeout=90,
            name=None,
            command_cwd=None,
            port_forwardings=(),
            mounts=(),
            reverse_tunnel=None,
            detached=False,
            failover=False,
            node_id=None,
            network_policy=None,
        )
        for policy in (None, NetworkPolicy()):
            with self.subTest(policy=policy):
                session = adx._Session(
                    native,
                    replace(spec, network_policy=policy),
                    self.backend._connection,
                )
                with self.assertRaises(BackendOperationError) as raised:
                    session.fork(1, None, 300, None)
                self.assertEqual(raised.exception.code, "NETWORK_POLICY_UNCONFIRMED")
        native.create_snapshot.assert_not_called()
        confirmed = adx._Session(
            native,
            replace(spec, network_policy=NetworkPolicy.block()),
            self.backend._connection,
        )
        self.assertTrue(confirmed._network_policy_known)

    def test_create_template_is_explicit_and_snapshot_position_stays_compatible(self):
        requests = []

        def deny(_client, request, **kwargs):
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json={"capabilities": ["environment-templates-v1"]},
                    request=request,
                )
            requests.append(request)
            return httpx.Response(
                403,
                json={
                    "message": "denied",
                    "error": {
                        "code": "PERMISSION_DENIED",
                        "retry": "never",
                        "outcome": "not_started",
                    },
                },
                request=request,
            )

        with (
            patch.object(public, "load_backend", return_value=self.backend),
            patch.object(httpx.Client, "send", autospec=True, side_effect=deny),
        ):
            for run in (
                lambda: Sandbox.create(template="tpl-a"),
                lambda: Sandbox.from_template("tpl-a"),
                lambda: Sandbox.create("snapshot-a"),
            ):
                with self.assertRaises(BackendOperationError):
                    run()
        for request in requests[:2]:
            body = json.loads(request.content)
            self.assertEqual(body["templateId"], "tpl-a")
            self.assertNotIn("snapshotId", body)
            self.assertNotIn("cpu", body)
            self.assertNotIn("memory", body)
        self.assertEqual(json.loads(requests[2].content)["snapshotId"], "snapshot-a")

    def test_old_service_cannot_silently_fresh_boot_a_template(self):
        requests = []

        def old(_client, request, **kwargs):
            requests.append(request)
            return httpx.Response(
                200, json={"capabilities": ["workload-probes-v1"]}, request=request
            )

        with (
            patch.object(public, "load_backend", return_value=self.backend),
            patch.object(httpx.Client, "send", autospec=True, side_effect=old),
        ):
            with self.assertRaises(UnsupportedBackendFeatureError):
                Sandbox.create(template="tpl-a")
        self.assertEqual([r.method for r in requests], ["GET"])

    def test_mutually_exclusive_inputs_fail_before_network(self):
        for kwargs in (
            {"snapshot_id": "s", "template": "tpl-a"},
            {"template": "tpl-a", "image": "image"},
            {"template": "tpl-a", "cpu": 1000},
        ):
            with (
                self.subTest(kwargs=kwargs),
                patch.object(httpx.Client, "send") as send,
                self.assertRaises(ValueError),
            ):
                Sandbox.create(**kwargs)
            send.assert_not_called()

    def test_catalog_wire_is_authenticated_paginated_and_checks_identity(self):
        requests = []
        info = {
            "templateId": "tpl-a",
            "snapshotId": "s",
            "name": "python",
            "status": "ready",
        }

        def reply(_client, request, **kwargs):
            requests.append(request)
            payload = (
                {"items": [info], "nextPageToken": "next"}
                if request.method == "GET" and request.url.path.endswith("/templates")
                else info
            )
            return httpx.Response(
                200, json={"code": 200, "data": payload}, request=request
            )

        with patch.object(httpx.Client, "send", autospec=True, side_effect=reply):
            self.backend.import_template("s", "python", "tpl-a")
            self.backend.get_template("tpl-a")
            rows, token = self.backend.list_templates("python", "previous", 1)
            self.backend.delete_template("tpl-a")
        self.assertEqual(rows, [TemplateInfo("tpl-a", "s", "python")])
        self.assertEqual(token, "next")
        self.assertEqual(
            dict(requests[2].url.params),
            {"name": "python", "pageToken": "previous", "pageSize": "1"},
        )
        self.assertTrue(all(r.headers.get("X-Auth") == "test-only" for r in requests))

    def test_unknown_import_retains_known_template_id_without_retry(self):
        with patch.object(
            httpx.Client, "send", side_effect=httpx.ReadTimeout("lost response")
        ) as send:
            with self.assertRaises(BackendOperationError) as raised:
                self.backend.import_template("s", "python", "tpl-known")
        self.assertEqual(send.call_count, 1)
        self.assertEqual(raised.exception.template_id, "tpl-known")


if __name__ == "__main__":
    unittest.main()
