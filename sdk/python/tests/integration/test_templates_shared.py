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

"""Staged shared-template acceptance against deployment-owned fault injection.

Set AKERNEL_RUN_INTEGRATION=1, AKERNEL_TEST_IMAGE, source/target node IDs in
AKERNEL_TEST_TEMPLATE_SOURCE_NODE / AKERNEL_TEST_TEMPLATE_TARGET_NODE,
AKERNEL_TEST_TEMPLATE_STORAGE_ALIAS, AKERNEL_TEST_TEMPLATE_RECEIPT and
AKERNEL_TEST_TEMPLATE_INSPECT_COMMAND (a JSON argv array). The read-only
inspection helper accepts environment/snapshot/node plus an ID, and returns
committed placement/storage with physical_host or node online status.

--capture-receipt creates a retained template; the tests leave it intact so an
operator can retire the source, recreate storage/control/node processes and
rerun from a fresh client. Explicitly clean the receipt's source/template/snapshot
after verification. This module never changes Kubernetes or other workloads.
"""

import json
import os
import subprocess
import sys
import time
import unittest
import uuid
from pathlib import Path

from akernel_sdk import (
    BackendOperationError,
    NotFoundException,
    Sandbox,
    Template,
    TemplateInfo,
)
from tests.integration import test_fork

_REQUIRED = (
    "AKERNEL_TEST_TEMPLATE_SOURCE_NODE",
    "AKERNEL_TEST_TEMPLATE_TARGET_NODE",
    "AKERNEL_TEST_TEMPLATE_STORAGE_ALIAS",
    "AKERNEL_TEST_TEMPLATE_RECEIPT",
    "AKERNEL_TEST_TEMPLATE_INSPECT_COMMAND",
)
_ENABLED = test_fork._ENABLED and all(os.environ.get(key) for key in _REQUIRED)


def inspect_record(kind, identity):
    command = json.loads(os.environ["AKERNEL_TEST_TEMPLATE_INSPECT_COMMAND"])
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(arg, str) and arg for arg in command)
    ):
        raise ValueError("inspection command must be a nonempty JSON argv array")
    result = subprocess.run(
        [*command, kind, identity],
        check=True,
        text=True,
        capture_output=True,
        timeout=45,
    )
    return json.loads(result.stdout)


def witness(sandbox, action="inspect"):
    return test_fork.ForkIntegrationTest.witness(unittest.TestCase(), sandbox, action)


def write_receipt(path, value):
    temporary = path.with_suffix(".pending")
    with temporary.open("w", encoding="utf-8") as file:
        os.chmod(temporary, 0o600)
        json.dump(value, file, indent=2)
        file.flush()
        os.fsync(file.fileno())
    temporary.replace(path)


def capture_receipt():
    if not _ENABLED or not test_fork._IMAGE:
        raise RuntimeError("enable shared acceptance and set a python3 image")
    source_node = os.environ["AKERNEL_TEST_TEMPLATE_SOURCE_NODE"]
    target_node = os.environ["AKERNEL_TEST_TEMPLATE_TARGET_NODE"]
    alias = os.environ["AKERNEL_TEST_TEMPLATE_STORAGE_ALIAS"]
    if source_node == target_node or alias == "local":
        raise ValueError("two distinct nodes and a shared storage alias are required")
    path = Path(os.environ["AKERNEL_TEST_TEMPLATE_RECEIPT"])
    with path.open("x", encoding="utf-8") as file:
        os.chmod(path, 0o600)
        file.write("{}")
    value = {
        "schema": 1,
        "template_id": "tpl-shared-" + uuid.uuid4().hex,
        "name": "shared-acceptance-" + uuid.uuid4().hex,
        "source_node": source_node,
        "target_node": target_node,
        "storage_alias": alias,
        "source_idle_timeout": 60,
    }
    write_receipt(path, value)
    source = Sandbox(
        image=test_fork._IMAGE,
        runtime=os.environ.get("AKERNEL_TEST_RUNTIME", "runsc"),
        cpu=1000,
        memory=512,
        storage_mb=256,
        idle_timeout=value["source_idle_timeout"],
        node_id=source_node,
        detached=True,
        schedule_timeout=180,
    )
    value["source_id"] = source.id
    write_receipt(path, value)
    try:
        placement = inspect_record("environment", source.id)
        if placement["node_id"] != source_node:
            raise AssertionError("source was not placed on the requested node")
        value["source_host"] = placement["physical_host"]
        source.commands.run("mkdir -p /tmp/fork-acceptance")
        source.files.write("/tmp/fork-acceptance/data", "0123456789abcdef")
        source.files.write("/tmp/fork-acceptance/marker", "captured")
        source.files.write("/tmp/fork-acceptance/worker.py", test_fork._WORKER)
        source.commands.run("python3 /tmp/fork-acceptance/worker.py", background=True)
        value["witness"] = witness(source)
        snapshot = source.create_snapshot(timeout=300)
        value["snapshot_id"] = snapshot.snapshot_id
        write_receipt(path, value)
        metadata = inspect_record("snapshot", snapshot.snapshot_id)
        if metadata["storage"] != alias or metadata["source_node_id"] != source_node:
            raise AssertionError("snapshot was not published to the shared store")
        template = Template.import_snapshot(
            snapshot, name=value["name"], template_id=value["template_id"]
        )
        if Template.get(template) != template:
            raise AssertionError("template registration was not confirmed")
        value["published_at_unix"] = time.time()
        value["ready"] = True
        write_receipt(path, value)
        source.files.write("/tmp/fork-acceptance/marker", "source-after-capture")
        witness(source, "advance")
        print("Retained shared template captured; receipt:", path, flush=True)
    finally:
        # Preserve the source until deployment-owned failure injection. The
        # receipt is deliberately kept even when an operation outcome is unknown.
        source.close()


@unittest.skipUnless(_ENABLED, "enable staged shared-template acceptance")
class SharedTemplateIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.receipt = json.loads(
            Path(os.environ["AKERNEL_TEST_TEMPLATE_RECEIPT"]).read_text()
        )
        self.assertEqual(self.receipt["schema"], 1)
        self.assertTrue(self.receipt["ready"])
        self.assertNotEqual(self.receipt["source_node"], self.receipt["target_node"])
        self.template = TemplateInfo(
            self.receipt["template_id"],
            self.receipt["snapshot_id"],
            self.receipt["name"],
        )
        self.assertEqual(Template.get(self.template), self.template)
        metadata = inspect_record("snapshot", self.receipt["snapshot_id"])
        self.assertEqual(metadata["storage"], self.receipt["storage_alias"])
        self.assertNotEqual(metadata["storage"], "local")

    def child(self, *, pin_target=True):
        child = Sandbox.create(
            template=self.template,
            node_id=self.receipt["target_node"] if pin_target else None,
            schedule_timeout=180,
            idle_timeout=600,
        )
        self.addCleanup(child.kill)
        placement = inspect_record("environment", child.id)
        self.assertEqual(placement["node_id"], self.receipt["target_node"])
        self.assertNotEqual(placement["physical_host"], self.receipt["source_host"])
        self.assertEqual(witness(child), self.receipt["witness"])
        self.assertEqual(child.files.read("/tmp/fork-acceptance/marker"), "captured")
        return child

    def test_cross_node_state_and_independent_writes(self):
        first, second = self.child(), self.child()
        self.assertNotEqual(first.id, second.id)
        before = self.receipt["witness"]
        after = witness(first, "advance")
        self.assertEqual(after["value"], before["value"] + 1)
        self.assertEqual(after["offset"], before["offset"] + 4)
        first.files.write("/tmp/fork-acceptance/marker", "child-one")
        self.assertEqual(witness(second), before)
        self.assertEqual(second.files.read("/tmp/fork-acceptance/marker"), "captured")
        with self.assertRaises(BackendOperationError) as busy:
            Sandbox.delete_snapshot(self.receipt["snapshot_id"])
        self.assertEqual(busy.exception.code, "SNAPSHOT_IN_USE")
        print(
            "proof: shared cross-host restore preserves memory/FD and isolates writes"
        )

    @unittest.skipUnless(
        os.environ.get("AKERNEL_TEST_TEMPLATE_SOURCE_OFFLINE") == "1",
        "deployment must first retire only the test-owned source node",
    )
    def test_retained_template_after_source_loss_and_restarts(self):
        source = inspect_record("node", self.receipt["source_node"])
        self.assertFalse(source["online"])
        try:
            state = Sandbox.get_info(self.receipt["source_id"]).state
        except NotFoundException:
            state = "deleted"
        self.assertIn(state, ("failed", "deleted"))
        age = time.time() - self.receipt["published_at_unix"]
        minimum = int(os.environ.get("AKERNEL_TEST_TEMPLATE_MIN_AGE_SECONDS", "0"))
        self.assertGreaterEqual(age, minimum)
        self.assertGreater(age, self.receipt["source_idle_timeout"])
        # Exercise scheduler fallback after source loss, without a target hint.
        child = self.child(pin_target=False)
        self.assertEqual(
            child.commands.run("printf restored-alive").stdout, "restored-alive"
        )
        print(
            "proof:",
            json.dumps(
                {
                    "case": "retained-shared-template",
                    "observed_retention_seconds": round(age, 3),
                    "source_offline": True,
                    "source_state": state,
                    "source_host": self.receipt["source_host"],
                    "target_node": self.receipt["target_node"],
                    "storage_alias": self.receipt["storage_alias"],
                    "default_placement_without_target_hint": True,
                    "memory_pid_fd_and_files_verified": True,
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    if sys.argv[1:] == ["--capture-receipt"]:
        capture_receipt()
    else:
        unittest.main()
