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

"""Opt-in template catalog, actual restore isolation, policy and deletion races.

Uses AKERNEL_RUN_INTEGRATION=1 and a python3 AKERNEL_TEST_IMAGE. Every resource
is test-owned; cleanup removes registrations before snapshots. The optional
AKERNEL_TEST_NETWORK_URL must be reachable from an unrestricted sandbox.
"""

import concurrent.futures
import json
import os
import shlex
import threading
import unittest
import uuid

from akernel_sdk import (
    BackendOperationError,
    NetworkPolicy,
    NotFoundException,
    Sandbox,
    Template,
)
from tests.integration import test_fork


@unittest.skipUnless(
    test_fork._ENABLED and test_fork._IMAGE,
    "enable integration and set a python3 image",
)
class TemplateIntegrationTest(test_fork.ForkIntegrationFixture, unittest.TestCase):
    witness = test_fork.ForkIntegrationTest.witness

    def setUp(self):
        super().setUp()
        self.template_ids = []
        self.label = "template-test-" + uuid.uuid4().hex
        self.addCleanup(self.cleanup_templates)

    def cleanup_templates(self):
        errors = []
        for identity in reversed(self.template_ids):
            try:
                Template.delete(identity)
                with self.assertRaises(NotFoundException):
                    Template.get(identity)
            except Exception as error:
                errors.append(error)
        if errors:
            raise RuntimeError("test-owned templates did not clean up") from errors[0]

    def register(self, snapshot):
        identity = "tpl-" + uuid.uuid4().hex
        self.template_ids.append(identity)
        return Template.import_snapshot(snapshot, name=self.label, template_id=identity)

    def test_catalog_memory_fd_isolation_and_independent_verifier(self):
        source = self.source
        source.commands.run("mkdir -p /tmp/fork-acceptance")
        source.files.write("/tmp/fork-acceptance/data", "0123456789abcdef")
        source.files.write("/tmp/fork-acceptance/marker", "captured")
        source.files.write("/tmp/fork-acceptance/worker.py", test_fork._WORKER)
        source.commands.run("python3 /tmp/fork-acceptance/worker.py", background=True)
        before = self.witness(source)
        snapshot = source.create_snapshot(timeout=300)
        first_template = self.register(snapshot)
        second_template = self.register(snapshot)
        self.assertEqual(
            Template.import_snapshot(
                snapshot, name=self.label, template_id=first_template.template_id
            ),
            first_template,
        )
        self.assertEqual(Template.get(first_template), first_template)
        pages = Template.list(name=self.label, limit=1)
        listed = []
        while pages.has_next:
            listed.extend(pages.next_items())
        self.assertEqual(
            {item.template_id for item in listed},
            {first_template.template_id, second_template.template_id},
        )
        self.assertEqual(len(listed), 2)
        with self.assertRaises(BackendOperationError) as busy:
            Sandbox.delete_snapshot(snapshot)
        self.assertEqual(busy.exception.code, "SNAPSHOT_IN_USE")
        Template.delete(second_template)
        source.files.write("/tmp/fork-acceptance/marker", "after-capture")
        self.witness(source, "advance")
        first = Sandbox.create(template=first_template, schedule_timeout=180)
        self.handles.append(first)
        second = Sandbox.from_template(first_template.template_id, schedule_timeout=180)
        self.handles.append(second)
        self.assertEqual(len({source.id, first.id, second.id}), 3)
        self.assertEqual(self.witness(first), before)
        self.assertEqual(self.witness(second), before)
        self.assertEqual(first.files.read("/tmp/fork-acceptance/marker"), "captured")
        changed = self.witness(first, "advance")
        self.assertEqual(changed["value"], before["value"] + 1)
        self.assertEqual(changed["offset"], before["offset"] + 4)
        self.assertEqual(self.witness(second), before)
        first.files.write("/tmp/fork-acceptance/marker", "child-one")
        self.assertEqual(second.files.read("/tmp/fork-acceptance/marker"), "captured")
        self.assertEqual(
            source.files.read("/tmp/fork-acceptance/marker"), "after-capture"
        )
        source.kill()
        self.assertEqual(Template.get(first_template), first_template)

        verifier = Sandbox(
            image=test_fork._IMAGE,
            runtime=os.environ.get("AKERNEL_TEST_RUNTIME", "runsc"),
            cpu=1000,
            memory=512,
            storage_mb=128,
            schedule_timeout=180,
        )
        self.handles.append(verifier)
        self.assertFalse(verifier.files.exists("/tmp/fork-acceptance"))
        verifier.files.write(
            "/tmp/result", first.files.read("/tmp/fork-acceptance/marker")
        )
        self.assertEqual(
            verifier.commands.run('test "$(cat /tmp/result)" = child-one').exit_code, 0
        )
        self.assertNotEqual(
            verifier.commands.run('test "$(cat /tmp/result)" = invalid').exit_code, 0
        )

        Template.delete(first_template)
        with self.assertRaises(NotFoundException):
            Template.get(first_template)
        with self.assertRaises(NotFoundException):
            Sandbox.create(template=first_template, schedule_timeout=180)
        # Unregister is not deletion of the caller-owned snapshot. Both legacy
        # entry points must still restore the same saved state on the real cluster.
        for restore in (Sandbox.create, Sandbox.from_snapshot):
            legacy = restore(snapshot, schedule_timeout=180)
            self.handles.append(legacy)
            self.assertEqual(self.witness(legacy), before)
            self.assertEqual(
                legacy.files.read("/tmp/fork-acceptance/marker"), "captured"
            )
            legacy.kill()
        self.assertEqual(self.witness(first), changed)
        self.assertEqual(self.witness(second), before)
        first.kill()
        self.assertEqual(self.witness(second), before)
        print(
            "proof:",
            json.dumps(
                {
                    "case": "template-lifecycle",
                    "templates": 2,
                    "children": 2,
                    "memory_pid_fd_preserved": True,
                    "writes_isolated": True,
                    "source_deleted": True,
                    "unregister_preserves_children": True,
                    "independent_verifier": True,
                    "legacy_snapshot_entry_points": 2,
                }
            ),
            flush=True,
        )

    @unittest.skipUnless(
        os.environ.get("AKERNEL_TEST_NETWORK_URL"),
        "set a reachable outbound policy fixture",
    )
    def test_policy_is_reapplied_and_cannot_be_overridden_at_creation(self):
        url = os.environ["AKERNEL_TEST_NETWORK_URL"]
        code = (
            "import urllib.request; print(urllib.request.build_opener("
            "urllib.request.ProxyHandler({})).open("
            + repr(url)
            + ", timeout=4).read().decode())"
        )
        command = "python3 -c " + shlex.quote(code)
        baseline = self.source.commands.run(command)
        self.assertEqual(baseline.exit_code, 0, baseline.stderr)
        self.assertIn("template-mvp-ready", baseline.stdout)
        self.source.update_network_policy(NetworkPolicy.block())
        snapshot = self.source.create_snapshot(timeout=300)
        item = self.register(snapshot)
        self.source.update_network_policy(None)
        self.assertEqual(self.source.commands.run(command).exit_code, 0)
        child = Sandbox.create(template=item, schedule_timeout=180)
        self.handles.append(child)
        self.assertNotEqual(child.commands.run(command).exit_code, 0)
        self.assertEqual(
            child.commands.run("printf control-alive").stdout, "control-alive"
        )
        empty_policy_child = Sandbox.create(
            template=item, network_policy=NetworkPolicy(), schedule_timeout=180
        )
        self.handles.append(empty_policy_child)
        self.assertNotEqual(empty_policy_child.commands.run(command).exit_code, 0)
        with self.assertRaises(BackendOperationError) as unconfirmed:
            empty_policy_child.fork()
        self.assertEqual(unconfirmed.exception.code, "NETWORK_POLICY_UNCONFIRMED")
        with self.assertRaises(BackendOperationError):
            Sandbox.create(
                template=item,
                network_policy=NetworkPolicy.deny_dns("override.invalid"),
                schedule_timeout=180,
            )
        print(
            "proof:",
            json.dumps(
                {
                    "case": "template-network-policy",
                    "outbound_baseline_reachable": True,
                    "captured_restrictions_reapplied": True,
                    "empty_policy_keeps_restrictions_and_blocks_unconfirmed_fork": True,
                    "creation_override_rejected": True,
                }
            ),
            flush=True,
        )

    def test_delete_and_create_have_a_durable_admission_boundary(self):
        snapshot = self.source.create_snapshot(timeout=300)
        accepted = 0
        for _ in range(3):
            item = self.register(snapshot)
            barrier = threading.Barrier(2)

            def create(item=item, barrier=barrier):
                barrier.wait(timeout=10)
                return Sandbox.create(template=item, schedule_timeout=180)

            def delete(item=item, barrier=barrier):
                barrier.wait(timeout=10)
                Template.delete(item)

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                started = executor.submit(create)
                removed = executor.submit(delete)
                removed.result()
                try:
                    child = started.result()
                except NotFoundException:
                    child = None
            if child is not None:
                self.handles.append(child)
                accepted += 1
                self.assertEqual(
                    child.commands.run("printf accepted-child").stdout, "accepted-child"
                )
            with self.assertRaises(NotFoundException):
                Template.get(item)
            with self.assertRaises(NotFoundException):
                Sandbox.create(template=item, schedule_timeout=180)
        print(
            "proof:",
            json.dumps(
                {
                    "case": "template-delete-race",
                    "attempts": 3,
                    "accepted_children_survive": accepted,
                    "late_admission_rejected": True,
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
