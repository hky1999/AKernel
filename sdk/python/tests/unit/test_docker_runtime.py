"""Shared lower integrity, private upper restore config and one-time startup."""

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx

from akernel_sdk import DockerRuntime, ImageMountPlan, Sandbox
from akernel_sdk import _image_resolver as resolver
from akernel_sdk import sandbox as public
from akernel_sdk._docker_runtime import _PREPARE_RUNTIME
from akernel_sdk._image_bridge_guest import verify_image_bridge


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


class DockerRuntimeTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def plan(self):
        blobs = []
        for kind in ("config", "manifest"):
            payload = json.dumps({"kind": kind}).encode()
            name = kind + ".json"
            (self.directory / name).write_bytes(payload)
            blobs.append(
                {
                    "file": name,
                    "descriptor": {"digest": digest(payload), "size": len(payload)},
                }
            )
        target = blobs[1]["descriptor"]
        reference = "registry.example/test@" + target["digest"]
        image = {
            "reference": reference,
            "root": "/opt/akernel-images/" + target["digest"][7:],
            "target": target,
            "config": blobs[0]["descriptor"],
            "blobs": blobs,
            "chain_id": "sha256:" + "a" * 64,
        }
        path = self.directory / "plan.json"
        path.write_text(
            json.dumps({"images": [image], "services": {"main": reference}})
        )
        return path

    def test_integrity_and_path_escape_rejected_before_sandbox_creation(self):
        for defect in ("corrupt", "escape", "root"):
            with self.subTest(defect=defect):
                path = self.plan()
                plan = json.loads(path.read_text())
                if defect == "corrupt":
                    (self.directory / "config.json").write_bytes(b"changed")
                elif defect == "escape":
                    plan["images"][0]["blobs"][0]["file"] = "../private"
                else:
                    plan["images"][0]["root"] += "different"
                path.write_text(json.dumps(plan))
                with self.assertRaises(ValueError):
                    ImageMountPlan.load(path)

    def test_restore_options_retain_lower_and_upper_without_startup(self):
        runtime = DockerRuntime(
            images=ImageMountPlan.load(self.plan()), writable_mb=2048
        )
        backend = MagicMock()
        backend.from_snapshot.return_value.id = "restored"
        with patch.object(public, "load_backend", return_value=backend):
            for factory in (Sandbox.from_snapshot, Sandbox.create):
                options = runtime.sandbox_options()
                sb = factory("snapshot", **options)
                spec = backend.from_snapshot.call_args.args[0]
                reference = runtime.images.images[0]["reference"]
                self.assertEqual(spec.mounts[0].image_url, reference)
                upper = options["extra_config"]["fileBackedTmpfsMounts"][0]
                self.assertEqual(
                    spec.extra_config["fileBackedTmpfsMounts"][0]["sizeMB"], 2048
                )
                upper["sizeMB"] = 999
                self.assertEqual(
                    spec.extra_config["fileBackedTmpfsMounts"][0]["sizeMB"], 2048
                )
                backend.create.assert_not_called()
                backend.from_snapshot.return_value.commands.run.assert_not_called()
                sb.kill()

    def test_start_checks_return_codes_and_refuses_existing_daemon(self):
        sandbox = MagicMock()
        sandbox.commands.run.side_effect = RuntimeError("existing daemon")
        with self.assertRaisesRegex(RuntimeError, "existing daemon"):
            DockerRuntime().start(sandbox)
        self.assertEqual(sandbox.commands.run.call_count, 1)
        self.assertTrue(sandbox.commands.run.call_args.kwargs["check"])
        sandbox.files.copy_from_local.assert_not_called()

    def test_public_runtime_bootstrap_installs_missing_tools_before_bridge(self):
        sandbox = MagicMock()
        runtime = DockerRuntime()
        with patch.object(DockerRuntime, "_bridge", return_value=[]) as bridge:
            # Refusing an existing daemon is the first operation. A failed package
            # preparation on a fresh sandbox must not start any image services.
            sandbox.commands.run.side_effect = [
                SimpleNamespace(),
                RuntimeError("bootstrap failed"),
            ]
            with self.assertRaisesRegex(RuntimeError, "bootstrap failed"):
                runtime.start(sandbox)
            bridge.assert_not_called()
        self.assertIn("apk add --no-cache", sandbox.commands.run.call_args.args[0])
        self.assertTrue(sandbox.commands.run.call_args.kwargs["check"])

    def test_bootstrap_reuses_ready_images_and_handles_public_alpine(self):
        binaries = self.directory / "bin"
        binaries.mkdir()
        record = self.directory / "packages"

        def executable(name, body="exit 0"):
            path = binaries / name
            path.write_text("#!/bin/sh\n" + body + "\n")
            path.chmod(0o755)

        for name in ("docker", "dockerd", "containerd", "ctr"):
            executable(name)
        executable("apk", 'printf "%s\\n" "$*" > "$RECORD"')
        environment = {"PATH": str(binaries), "RECORD": str(record)}
        result = subprocess.run(
            ["/bin/sh", "-c", _PREPARE_RUNTIME],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            record.read_text().strip(),
            "add --no-cache bash python3 coreutils util-linux shadow tar xz",
        )
        record.unlink()
        for name in (
            "bash",
            "python3",
            "findmnt",
            "mount",
            "tar",
            "xz",
            "realpath",
            "usermod",
        ):
            executable(name)
        result = subprocess.run(
            ["/bin/sh", "-c", _PREPARE_RUNTIME],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(record.exists())
        (binaries / "dockerd").unlink()
        result = subprocess.run(
            ["/bin/sh", "-c", _PREPARE_RUNTIME],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 127)
        self.assertIn("missing dockerd", result.stderr)
        self.assertFalse(record.exists())

    def test_bridge_upload_is_sdk_owned_and_precedes_dockerd(self):
        sandbox = MagicMock()
        sandbox.commands.run.return_value = SimpleNamespace(
            stdout='["--config-file", "/bridge/docker.json"]', stderr=""
        )
        binary = self.directory / "bridge"
        binary.write_bytes(b"fixture")
        runtime = DockerRuntime(
            images=ImageMountPlan.load(self.plan()), bridge_binary=binary
        )
        runtime.start(sandbox, network="host")
        commands = [c.args[0] for c in sandbox.commands.run.call_args_list]
        start = next(i for i, c in enumerate(commands) if "start.py --root" in c)
        docker = next(i for i, c in enumerate(commands) if "dockerd --config-file" in c)
        self.assertLess(start, docker)
        uploads = [c.args[0] for c in sandbox.files.copy_from_local.call_args_list]
        self.assertTrue(
            any(p.endswith("akernel_sdk/_image_bridge_guest.py") for p in uploads)
        )
        self.assertFalse(any("harbor" in p for p in uploads))

    def test_registry_auth_pins_metadata_and_never_downloads_layers(self):
        config = json.dumps(
            {
                "os": "linux",
                "architecture": "amd64",
                "rootfs": {"diff_ids": ["sha256:" + "a" * 64]},
            }
        ).encode()
        manifest = json.dumps(
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": {
                    "mediaType": "application/vnd.oci.image.config.v1+json",
                    "digest": digest(config),
                    "size": len(config),
                },
                "layers": [{"digest": "sha256:" + "b" * 64}],
            }
        ).encode()
        calls = []

        def handle(request):
            calls.append(request)
            if request.url.host == "auth.example":
                return httpx.Response(200, json={"token": "test-only"})
            if request.headers.get("Authorization") != "Bearer test-only":
                return httpx.Response(
                    401,
                    headers={
                        "www-authenticate": 'Bearer realm="https://auth.example/token",'
                        'scope="repository:library/test:pull"'
                    },
                )
            if "/manifests/" in request.url.path:
                return httpx.Response(
                    200,
                    content=manifest,
                    headers={"docker-content-digest": digest(manifest)},
                )
            if request.url.path.endswith(digest(config)):
                return httpx.Response(200, content=config)
            raise AssertionError("must never request a layer")

        with patch.dict("os.environ", {"DOCKER_CONFIG": str(self.directory)}):
            with httpx.Client(transport=httpx.MockTransport(handle)) as client:
                image = resolver.RegistryMetadata(client, self.directory).prepare(
                    "docker.io/library/test:latest"
                )
        self.assertEqual(
            image["reference"], "docker.io/library/test@" + digest(manifest)
        )
        self.assertEqual(len(image["blobs"]), 2)
        self.assertEqual(sum(r.url.host == "auth.example" for r in calls), 1)

    def test_prepared_image_verification_checks_alias_digest_and_committed_root(self):
        path = self.plan()
        data = json.loads(path.read_text())
        image = data["images"][0]
        alias = "registry.example/test:tag@" + image["target"]["digest"]
        data["aliases"] = {alias: image["reference"]}
        path.write_text(json.dumps(data))
        calls = []

        def run(args, **kwargs):
            calls.append(args)
            self.assertTrue(kwargs["check"])
            if args[0] == "ctr":
                payload = json.dumps(
                    {
                        "Kind": "Committed",
                        "Labels": {
                            "containerd.io/snapshot/agent-service.image-root": image[
                                "root"
                            ]
                        },
                    }
                )
            else:
                self.assertEqual(args[:3], ["docker", "image", "inspect"])
                payload = image["target"]["digest"] + "\n"
            return SimpleNamespace(stdout=payload)

        with patch("akernel_sdk._image_bridge_guest.subprocess.run", side_effect=run):
            verify_image_bridge(path)
        self.assertEqual(calls[0][-1], image["chain_id"])
        self.assertEqual([c[-1] for c in calls[1:]], [image["reference"], alias])

    def test_prepared_image_verification_rejects_missing_or_changed_inputs(self):
        path = self.plan()
        image = json.loads(path.read_text())["images"][0]
        valid = json.dumps(
            {
                "Kind": "Committed",
                "Labels": {
                    "containerd.io/snapshot/agent-service.image-root": image["root"]
                },
            }
        )
        for results, error_type in (
            ([subprocess.CalledProcessError(1, "ctr")], subprocess.CalledProcessError),
            (
                [
                    SimpleNamespace(stdout=valid),
                    subprocess.CalledProcessError(1, "docker"),
                ],
                subprocess.CalledProcessError,
            ),
            (
                [
                    SimpleNamespace(stdout=valid),
                    SimpleNamespace(stdout="sha256:" + "0" * 64),
                ],
                RuntimeError,
            ),
            (
                [SimpleNamespace(stdout=valid.replace("Committed", "Active"))],
                RuntimeError,
            ),
            (
                [SimpleNamespace(stdout=valid.replace(image["root"], "/other"))],
                RuntimeError,
            ),
        ):
            with self.subTest(results=results):
                with patch(
                    "akernel_sdk._image_bridge_guest.subprocess.run",
                    side_effect=results,
                ):
                    with self.assertRaises(error_type):
                        verify_image_bridge(path)
