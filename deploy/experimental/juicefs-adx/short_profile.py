#!/usr/bin/env python3
"""Task-private /run and bounded runtime socket paths; preserve original profiles."""
import argparse
import json
import hashlib
import stat
import os
from pathlib import Path
import subprocess
import sys
import tomllib

from lab import LabError, checked_root, private_json
from runtime_nodes import RuntimeNodes, scope


def copy_private_resolver(run_view: Path, relative: Path, data: bytes) -> dict:
    """Seed only task-owned directories, without following symlinks."""
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise LabError("invalid private resolver relative path")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    directory = os.open(run_view, flags)
    created = False
    try:
        for part in relative.parts[:-1]:
            try:
                os.mkdir(part, 0o700, dir_fd=directory)
            except FileExistsError:
                pass
            child = os.open(part, flags, dir_fd=directory)
            os.close(directory)
            directory = child
        name = relative.parts[-1]
        try:
            descriptor = os.open(
                name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600, dir_fd=directory,
            )
        except FileExistsError:
            descriptor = os.open(
                name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory,
            )
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o444:
                    raise LabError("existing private resolver is not a read-only regular file")
                if stream.read() != data:
                    raise LabError("existing private resolver differs; refusing overwrite")
        else:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fchmod(stream.fileno(), 0o444)
                os.fsync(stream.fileno())
            os.fsync(directory)
            created = True
    finally:
        os.close(directory)
    return {
        "relative_path": str(relative), "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(), "created": created,
    }


def seed_resolver_views(root: Path, generation: str, expected_sha256: str | None = None) -> dict:
    """Preserve the host resolver when private /run hides its symlink target."""
    source = Path("/etc/resolv.conf")
    resolved = source.resolve(strict=True)
    data = source.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise LabError("host resolver changed; refusing to seed a different configuration")
    if not data:
        raise LabError("host resolver is empty")
    result = {"source_target": str(resolved), "sha256": digest, "nodes": {}}
    try:
        relative = resolved.relative_to("/run")
    except ValueError:
        result["needed"] = False
        return result
    for node in ("source", "target"):
        scope(root, generation, node)
        RuntimeNodes(root, generation, node).verified()
    for node in ("source", "target"):
        result["nodes"][node] = copy_private_resolver(
            root / generation / node / "run-view", relative, data,
        )
    result["needed"] = True
    return result


ALIAS = Path("/run/a0")
SANDBOX_ID = "sbox-00000000-0000-0000-0000-000000000000"


def runtime_root(node):
    if node not in ("source", "target"):
        raise LabError("invalid short-profile node")
    return ALIAS / ("s" if node == "source" else "t") / "r"


def socket_lengths(root):
    paths = {"runsc": root / "runsc" / ("runsc-" + SANDBOX_ID + ".sock")}
    fc = Path("/run/sandboxd/firecracker") / ("0" * 24)
    paths.update({"fc_" + name: fc / name for name in ("api.sock", "agent.vsock", "virtiofs.sock")})
    result = {name: {"path": str(path), "bytes": len(os.fsencode(path))}
              for name, path in paths.items()}
    if any(value["bytes"] >= (100 if name.startswith("fc_") else 108)
           for name, value in result.items()):
        raise LabError("runtime Unix socket path exceeds its supported limit")
    return result


def render_short_toml(text, original, short):
    if tomllib.loads(text).get("rootDir") != str(original):
        raise LabError("original sandboxd root does not match this task")
    old, new = json.dumps(str(original)), json.dumps(str(short))
    if text.count(old) != 1:
        raise LabError("expected exactly one original root literal")
    rendered = text.replace(old, new)
    if tomllib.loads(rendered).get("rootDir") != str(short):
        raise LabError("short root rendering failed")
    socket_lengths(short)
    return rendered


def render_stable_toml(text, original, short, expected_filestore):
    rendered = render_short_toml(text, original, short)
    parsed = tomllib.loads(rendered)
    runtime = parsed.get("plugin", {}).get("runtime", {})
    if runtime.get("filestore_dir") != str(expected_filestore):
        raise LabError("original filestore does not match this task node")
    old, new = json.dumps(str(expected_filestore)), json.dumps(str(ALIAS / "filestore"))
    if rendered.count(old) != 1:
        raise LabError("expected exactly one original filestore literal")
    rendered = rendered.replace(old, new)
    if tomllib.loads(rendered)["plugin"]["runtime"]["filestore_dir"] != str(ALIAS / "filestore"):
        raise LabError("stable filestore rendering failed")
    return rendered


def activate_filestore(root, generation, node):
    runtime = RuntimeNodes(root, generation, node)
    owner = runtime.owner_inside()
    runtime.validate_inside(owner)
    ready = json.loads(runtime.ready_path.read_text())
    pids = set()
    for path in Path("/sys/fs/cgroup").rglob("cgroup.procs"):
        pids.update(map(int, path.read_text().split()))
    if pids != {ready["pid"], os.getpid()}:
        raise LabError("stable filestore bind requires stopped task services")
    mounts = Path("/proc/self/mountinfo").read_text().splitlines()
    for line in mounts:
        optional = line.split(" - ", 1)[0].split()[6:]
        if any(field.startswith(("shared:", "master:")) for field in optional):
            raise LabError("task mount namespace is not recursively private")
    identity = lambda path: (path.stat().st_dev, path.stat().st_ino)
    if ALIAS.is_symlink() or identity(ALIAS) != identity(root / generation):
        raise LabError("stable filestore requires the verified task short alias")
    source = root / "mount" / "runtime" / generation / node / "filestore"
    destination = ALIAS / "filestore"
    output = root / generation / node / "filestore-alias-ready.json"
    if output.exists() or any(line.split(" - ", 1)[0].split()[4] == str(destination) for line in mounts):
        raise LabError("stable filestore already activated; refusing repeated mutation")
    if source.is_symlink() or not source.is_dir() or destination.is_symlink():
        raise LabError("filestore bind requires real task-owned directories")
    destination.mkdir(mode=0o700, exist_ok=True)
    subprocess.run(["mount", "--bind", str(source), str(destination)], check=True, timeout=10)
    if identity(source) != identity(destination):
        raise LabError("stable filestore backing directory identity mismatch")
    runtime.validate_inside(owner)
    summary = {"task": root.name, "generation": generation, "node": node,
               "source": str(source), "alias": str(destination),
               "device": source.stat().st_dev, "inode": source.stat().st_ino,
               "mount_namespace": os.stat("/proc/self/ns/mnt").st_ino, "ready": True}
    private_json(output, summary)
    return summary


def prepare(root, generation):
    base = root / generation
    record_path = base / "short-profile.json"
    if record_path.exists():
        raise LabError("short profile already exists; previous scene retained")
    record = {"task": root.name, "generation": generation, "alias": str(ALIAS), "nodes": {}}
    rendered = {}
    for node in ("source", "target"):
        runtime = RuntimeNodes(root, generation, node)
        owner = runtime.verified()
        pids = set()
        for path in runtime.cgroup.rglob("cgroup.procs"):
            pids.update(map(int, path.read_text().split()))
        if pids != {owner["pid"]}:
            raise LabError("stop task services before preparing short profile")
        directory = base / node
        short = runtime_root(node)
        rendered[node] = render_stable_toml((directory / "sandboxd.toml").read_text(),
            directory / "sandboxd-root", short,
            root / "mount" / "runtime" / generation / node / "filestore")
        record["nodes"][node] = {"runtime_root": str(short),
            "original_root": str(directory / "sandboxd-root"),
            "run_view": str(directory / "run-view"),
            "sockets": socket_lengths(short)}
    for node in ("source", "target"):
        directory = base / node
        run_view = Path(record["nodes"][node]["run_view"])
        run_view.mkdir(mode=0o700)
        (run_view / "a0").mkdir(mode=0o700)
        (base / ("s" if node == "source" else "t")).mkdir(mode=0o700)
        (base / ("s" if node == "source" else "t") / "r").mkdir(mode=0o700)
        with (directory / "sandboxd-short.toml").open("x") as stream:
            os.chmod(stream.name, 0o600)
            stream.write(rendered[node])
        deployment = json.loads((directory / "deployment.yaml").read_text())
        deployment["state_dir"] = str(directory / "state-short")
        if (directory / "deployment-short.yaml").exists():
            raise LabError("refusing to overwrite short deployment")
        private_json(directory / "deployment-short.yaml", deployment)
    record["resolver"] = seed_resolver_views(root, generation)
    private_json(record_path, record)
    return record


def activate(root, generation, node):
    runtime = RuntimeNodes(root, generation, node)
    owner = runtime.owner_inside()
    runtime.validate_inside(owner)
    ready = json.loads(runtime.ready_path.read_text())
    pids = set()
    for path in Path("/sys/fs/cgroup").rglob("cgroup.procs"):
        pids.update(map(int, path.read_text().split()))
    if pids != {ready["pid"], os.getpid()}:
        raise LabError("short binds require only the task keeper and this executor")
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        optional = line.split(" - ", 1)[0].split()[6:]
        if any(field.startswith(("shared:", "master:")) for field in optional):
            raise LabError("task mount namespace is not recursively private")
    base = root / generation
    plan = json.loads((base / "short-profile.json").read_text())
    if plan["task"] != root.name or plan["generation"] != generation or plan["alias"] != str(ALIAS):
        raise LabError("short profile identity mismatch")
    item = plan["nodes"][node]
    run_view = base / node / "run-view"
    original = base / node / "sandboxd-root"
    short = runtime_root(node)
    if item["run_view"] != str(run_view) or item["original_root"] != str(original):
        raise LabError("short profile source mismatch")
    output = base / node / "short-profile-ready.json"
    if output.exists():
        raise LabError("short binds already activated; refusing repeated mutation")
    for path in (base, run_view, original, base / short.parent.name):
        if path.is_symlink() or not path.is_dir():
            raise LabError("short bind source must be a task-owned real directory")
    subprocess.run(["mount", "--bind", str(run_view), "/run"], check=True, timeout=10)
    subprocess.run(["mount", "--bind", str(base), str(ALIAS)], check=True, timeout=10)
    subprocess.run(["mount", "--bind", str(original), str(short)], check=True, timeout=10)
    identity = lambda path: (path.stat().st_dev, path.stat().st_ino)
    if identity(Path("/run")) != identity(run_view) or identity(ALIAS) != identity(base) or identity(short) != identity(original):
        raise LabError("short bind backing directory identity mismatch")
    runtime.validate_inside(owner)
    summary = {"task": root.name, "generation": generation, "node": node,
               "private_run": str(run_view), "runtime_root": str(short),
               "original_root": str(original), "runtime_inode": original.stat().st_ino,
               "mount_namespace": os.stat("/proc/self/ns/mnt").st_ino,
               "sockets": socket_lengths(short), "ready": True}
    summary["filestore"] = activate_filestore(root, generation, node)
    private_json(output, summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--generation", required=True)
    parser.add_argument("--node", choices=("source", "target"))
    parser.add_argument("action", choices=("prepare", "activate", "_activate", "filestore", "_filestore"))
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise LabError("task profile activation requires root")
    root = checked_root(args.root)
    scope(root, args.generation, args.node or "source")
    if args.action == "prepare":
        result = prepare(root, args.generation)
    elif not args.node:
        raise LabError("activation requires a node")
    elif args.action in ("activate", "filestore"):
        RuntimeNodes(root, args.generation, args.node).execute([
            sys.executable, str(Path(__file__).resolve()), "--root", str(root),
            "--generation", args.generation, "--node", args.node,
            "_activate" if args.action == "activate" else "_filestore"])
        return
    elif args.action == "_filestore":
        result = activate_filestore(root, args.generation, args.node)
    else:
        result = activate(root, args.generation, args.node)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (LabError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print("short-profile error: " + str(error), file=sys.stderr)
        sys.exit(1)
