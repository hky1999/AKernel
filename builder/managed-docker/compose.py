"""Runtime-side Compose/Buildx adapter. Docker owns Dockerfile/Compose semantics.

The client uploads a context and invokes this tool; image metadata and ROFS
registration remain on the runtime side. This preview uses an operator-issued
session capability and pins its complete collection until session cleanup.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener


def execute(args: list[str], *, capture: bool = False) -> str:
    result = subprocess.run(
        args, check=True, text=True, stdout=subprocess.PIPE if capture else None
    )
    return result.stdout if capture else ""


def compose_command(descriptor: dict) -> list[str]:
    command = [
        "docker",
        "compose",
        "-p",
        descriptor["project_name"],
        "--project-directory",
        descriptor["directory"],
    ]
    for filename in descriptor["files"]:
        command.extend(["-f", filename])
    return command


def build_plan(
    descriptor: dict,
    config: dict,
    bake: dict,
    registry: str,
    prefix: str,
    no_cache: bool = False,
    registry_insecure: bool = False,
) -> tuple[dict, dict[str, str]]:
    """Adapt native Bake targets' output only; do not interpret Dockerfiles."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9/_-]*", prefix) or ".." in prefix:
        raise ValueError("Invalid session repository prefix")
    targets = bake.setdefault("target", {})
    references = {}
    for name, service in config["services"].items():
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", name):
            raise ValueError("Unsupported Compose service name")
        target = targets.get(name)
        if target is None:
            # Image-only inputs use the official Dockerfile frontend for format
            # conversion, retaining OCI config. No Python layer reconstruction.
            image = service.get("image")
            if not isinstance(image, str) or any(c.isspace() for c in image):
                raise ValueError("Service requires an image or native build target")
            target = {"dockerfile-inline": "FROM " + image + "\n"}
            targets[name] = target
        reference = registry + "/" + prefix.rstrip("/") + "/" + name.lower() + ":built"
        references[name] = reference
        target.update(
            {
                "tags": [reference],
                "platforms": ["linux/amd64"],
                "attest": [],
                "output": [
                    "type=image,push=true,compression=nydus,force-compression=true,"
                    "oci-mediatypes=true,fs-version=5"
                    + (",registry.insecure=true" if registry_insecure else "")
                ],
            }
        )
        if no_cache:
            target["no-cache"] = True
    bake["group"] = {"default": {"targets": list(references)}}
    return bake, references


def build(descriptor: dict, profile: dict, *, no_cache: bool = False) -> dict:
    os.environ.update(descriptor.get("environment", {}))
    command = compose_command(descriptor)
    config = json.loads(execute([*command, "config", "--format", "json"], capture=True))
    bake = json.loads(execute([*command, "build", "--print"], capture=True))
    bake, targets = build_plan(
        descriptor,
        config,
        bake,
        profile["registry"],
        profile["prefix"],
        no_cache,
        profile.get("registry_insecure", False),
    )
    root = Path(descriptor["directory"] + ".build")
    root.mkdir(mode=0o700, exist_ok=True)
    plan_path = root / "bake.json"
    plan_path.write_text(json.dumps(bake))
    metadata_path = root / "metadata.json"
    execute(
        [
            "docker",
            "buildx",
            "bake",
            "--builder",
            profile["builder"],
            "--file",
            str(plan_path),
            "--metadata-file",
            str(metadata_path),
            "--progress",
            "plain",
            "default",
        ]
    )
    metadata = json.loads(metadata_path.read_text())
    references = {}
    for name, tagged in targets.items():
        digest = metadata[name]["containerimage.digest"]
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError("BuildKit did not return an immutable image digest")
        references[name] = tagged.rsplit(":", 1)[0] + "@" + digest
    (root / "artifacts.json").write_text(json.dumps(references, indent=2))
    return references


def prepare(descriptor: dict, profile: dict) -> dict:
    root = Path(descriptor["directory"] + ".build")
    references = json.loads((root / "artifacts.json").read_text())
    token = Path(profile["token_file"]).read_text().strip()
    opener = build_opener(ProxyHandler({}))
    prepared = []
    for reference in dict.fromkeys(references.values()):
        request = Request(
            profile["provider"] + "/prepare",
            data=json.dumps({"reference": reference}).encode(),
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
            },
        )
        with opener.open(request, timeout=180) as response:
            prepared.append(json.loads(response.read(16 * 1024)))
    execute(
        [
            "/opt/akernel-image-bridge",
            "--mode",
            "register",
            "--plan",
            profile["plan"],
            "--containerd",
            "/run/akernel-image-bridge/containerd.sock",
        ]
    )
    # Generated override; never mutate the supplied Compose or Dockerfiles.
    lines = ["services:"]
    for service, reference in references.items():
        lines.extend(
            [
                "  " + json.dumps(service) + ":",
                "    build: !reset null",
                "    image: " + json.dumps(reference),
                "    pull_policy: never",
            ]
        )
    overlay = str(root / "runtime-images.yaml")
    Path(overlay).write_text("\n".join(lines) + "\n")
    attached = dict(descriptor)
    attached["files"] = [*descriptor["files"], overlay]
    attached["managed_artifacts"] = references
    (root / "attached.json").write_text(json.dumps(attached, indent=2))
    return {"artifacts": references, "prepared": prepared, "descriptor": attached}


def start_runtime(profile: dict) -> None:
    import sys

    sys.path.insert(0, "/usr/lib/akernel")
    from docker_network_guest import configure
    from image_bridge_guest import start_image_bridge

    if Path("/var/run/docker.sock").exists():
        raise RuntimeError("Runtime startup is fresh-only")
    runtime = Path("/opt/akernel-docker-runtime")
    runtime.mkdir(exist_ok=True)
    flags = start_image_bridge(
        runtime,
        Path(profile["plan"]),
        [],
        writable_tmpfs_mb=profile["writable_mb"],
        writable_backing="file",
    )
    configure()
    env = dict(os.environ)
    env["PATH"] = str(runtime / "network-bin") + ":" + env["PATH"]
    with Path("/var/log/akernel-dockerd.log").open("a") as logs:
        process = subprocess.Popen(
            [
                "dockerd",
                *flags,
                "--iptables=false",
                "--ip6tables=false",
                "--userland-proxy=true",
            ],
            stdout=logs,
            stderr=logs,
            env=env,
            start_new_session=True,
        )
    import time

    for _ in range(40):
        if process.poll() is not None:
            raise RuntimeError("Docker exited; inspect /var/log/akernel-dockerd.log")
        if (
            subprocess.run(
                ["docker", "info"], capture_output=True, check=False
            ).returncode
            == 0
        ):
            break
        time.sleep(0.5)
    else:
        raise RuntimeError("Docker readiness timed out")
    execute(
        [
            "docker",
            "buildx",
            "create",
            "--name",
            profile["builder"],
            "--driver",
            "remote",
            profile["endpoint"],
            "--use",
        ]
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["start", "build", "prepare"])
    parser.add_argument("--profile", default="/run/akernel-build/profile.json")
    parser.add_argument("--descriptor")
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args()
    profile = json.loads(Path(args.profile).read_text())
    if args.operation == "start":
        start_runtime(profile)
    else:
        descriptor = json.loads(Path(args.descriptor).read_text())
        result = (
            build(descriptor, profile, no_cache=args.no_cache)
            if args.operation == "build"
            else prepare(descriptor, profile)
        )
        print(json.dumps(result))
