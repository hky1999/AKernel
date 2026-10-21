#!/usr/bin/env python3
"""Own-node cgroup, mount and network views for the independent native A0 lab."""
from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import time
import uuid

from lab import Lab, LabError, checked_root, private_json, process_start
from node_network import namespace_matches


CGROUP_ROOT = Path("/sys/fs/cgroup")
REQUIRED = {"cpu", "memory", "pids"}


def scope(root: Path, generation: str, node: str) -> Path:
    if not re.fullmatch(r"native[0-9]{2}", generation) or node not in ("source", "target"):
        raise LabError("invalid runtime-node generation or node")
    return CGROUP_ROOT / (root.name + "-" + generation + "-" + node)


def require_controllers(available: str, enabled: str) -> list[str]:
    inherited = set(available.split()) & set(enabled.split())
    if not REQUIRED <= inherited:
        raise LabError("host must already delegate cpu, memory and pids; root settings unchanged")
    return sorted(inherited)


def entered_command(pid: int, helper: Path, root: Path, generation: str,
                    node: str, command: list[str]) -> list[str]:
    if pid <= 1 or not command:
        raise LabError("a live keeper and a command are required")
    return ["nsenter", "--target", str(pid), "--mount", "--net", "--cgroup", "--",
            sys.executable, str(helper), "--root", str(root), "--generation", generation,
            "--node", node, "_exec", "--", *command]


def check_view(actual_inode: int, expected_inode: int, magic: str, membership: str) -> None:
    if actual_inode != expected_inode or magic.strip().lower() != "63677270":
        raise LabError("runtime cgroup mount is not this task's own subtree")
    if membership.strip() != "0::/control":
        raise LabError("runtime process is not in its own management cgroup")


def sysfs_commands(parent_namespace: int, current_namespace: int, mountinfo: str) -> list[list[str]]:
    if parent_namespace <= 0 or current_namespace <= 0 or parent_namespace == current_namespace:
        raise LabError("sysfs replacement requires a newly private mount namespace")
    found_sys = False
    for line in mountinfo.splitlines():
        fields = line.split(" - ", 1)[0].split()
        if len(fields) < 6:
            raise LabError("invalid mountinfo before sysfs replacement")
        if fields[4] == "/sys" or fields[4].startswith("/sys/"):
            found_sys |= fields[4] == "/sys"
            if any(field.startswith(("shared:", "master:")) for field in fields[6:]):
                raise LabError("sysfs mount propagation is not private")
    if not found_sys:
        raise LabError("inherited sysfs mount is absent")
    # Remove inherited children before covering /sys; overmounting alone leaves
    # hidden cgroup entries in mountinfo that confuse capacity observation.
    return [["umount", "--recursive", "/sys"],
            ["mount", "-t", "sysfs", "-o", "nosuid,nodev,noexec", "sysfs", "/sys"],
            ["mount", "-t", "cgroup2", "-o", "nosuid,nodev,noexec", "none", str(CGROUP_ROOT)]]


def check_cgroup_mounts(mountinfo: str) -> None:
    mounts = []
    for line in mountinfo.splitlines():
        before, separator, after = line.partition(" - ")
        if not separator or len(before.split()) < 6 or not after.split():
            raise LabError("invalid runtime mountinfo")
        fields, types = before.split(), after.split()
        if types[0] in ("cgroup", "cgroup2"):
            mounts.append((types[0], fields[3], fields[4]))
    if mounts != [("cgroup2", "/", str(CGROUP_ROOT))]:
        raise LabError("runtime requires exactly one task-rooted cgroup2 mount; hidden mounts rejected")


def unshare(flags: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.unshare.argtypes = [ctypes.c_int]
    libc.unshare.restype = ctypes.c_int
    if libc.unshare(flags):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def live(pid: int, start: str) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        return process_start(pid) == start and state != "Z"
    except FileNotFoundError:
        return False


class RuntimeNodes:
    def __init__(self, root: Path, generation: str, node: str):
        self.root, self.generation, self.node = root, generation, node
        self.cgroup = scope(root, generation, node)
        self.base = root / generation / node
        self.owner_path = self.base / "runtime-node-owner.json"
        self.ready_path = self.base / "runtime-node-ready.json"
        self.launch_path = self.base / "runtime-node-launch.json"

    def owner(self) -> dict:
        data = json.loads(self.owner_path.read_text())
        if (data.get("task") != self.root.name or data.get("generation") != self.generation
                or data.get("node") != self.node or data.get("cgroup") != str(self.cgroup)
                or self.cgroup.is_symlink()
                or self.cgroup.stat().st_ino != data.get("cgroup_inode")):
            raise LabError("runtime-node owner identity mismatch")
        return data

    def verified(self) -> dict:
        owner = self.owner()
        data = json.loads(self.ready_path.read_text())
        if data.get("nonce") != owner["nonce"] or not live(data["pid"], data["start"]):
            raise LabError("runtime-node keeper is absent or replaced")
        for name in ("mnt", "net", "cgroup"):
            if os.stat(f'/proc/{data["pid"]}/ns/{name}').st_ino != data["namespaces"][name]:
                raise LabError("runtime-node namespace identity changed")
        return data

    def setup(self, volume: str) -> dict:
        volume = str(uuid.UUID(volume))
        if not self.base.is_dir() or self.owner_path.exists() or self.ready_path.exists():
            raise LabError("new prepared generation required; previous scene preserved")
        controllers = require_controllers((CGROUP_ROOT / "cgroup.controllers").read_text(),
                                          (CGROUP_ROOT / "cgroup.subtree_control").read_text())
        lab = Lab(self.root)
        network = json.loads((self.root / "node-network.json").read_text())
        if network.get("task") != self.root.name or not network.get("ready"):
            raise LabError("task-owned ready network required")
        net = network["nodes"][self.node]
        netpath = Path("/run/netns") / net["name"]
        if not namespace_matches(netpath, net):
            raise LabError("network namespace identity mismatch")
        self.cgroup.mkdir(mode=0o755)  # Exclusive: never adopt a pre-existing host cgroup.
        owner = {"task": self.root.name, "generation": self.generation, "node": self.node,
                 "nonce": secrets.token_hex(16), "cgroup": str(self.cgroup),
                 "cgroup_inode": self.cgroup.stat().st_ino, "controllers": controllers,
                 "netns": net["name"], "netns_inode": netpath.stat().st_ino,
                 "volume_uuid": volume}
        private_json(self.owner_path, owner)
        helper = Path(__file__).resolve()
        command = lab.namespace_command(self.node, ["nsenter", "--net=" + str(netpath), "--",
            sys.executable, str(helper), "--root", str(self.root), "--generation", self.generation,
            "--node", self.node, "_keeper", owner["nonce"]])
        log = self.base / "runtime-node-keeper.log"
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stream,
                                     stderr=subprocess.STDOUT, start_new_session=True)
        private_json(self.launch_path, {"pid": child.pid, "start": process_start(child.pid),
                                       "log": str(log), "nonce": owner["nonce"]})
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise LabError("runtime-node keeper exited; private log: " + str(log))
            if self.ready_path.exists():
                return self.verified()
            time.sleep(0.1)
        raise LabError("runtime-node keeper readiness timeout; task scene retained")

    def keeper(self, nonce: str) -> None:
        owner = self.owner()
        if nonce != owner["nonce"] or os.stat("/proc/self/ns/net").st_ino != owner["netns_inode"]:
            raise LabError("keeper launch identity mismatch")
        parent_mount_namespace = os.stat("/proc/self/ns/mnt").st_ino
        unshare(0x00020000)  # CLONE_NEWNS; preserve the JuiceFS mount, privatize changes.
        subprocess.run(["mount", "--make-rprivate", "/"], check=True, timeout=10)
        control = self.cgroup / "control"
        control.mkdir()
        (self.cgroup / "cgroup.procs").write_text(str(os.getpid()))
        unshare(0x02000000)  # CLONE_NEWCGROUP: namespace root is the task's own cgroup.
        (control / "cgroup.procs").write_text(str(os.getpid()))
        # The node root is now empty; only its control child contains this keeper.
        (self.cgroup / "cgroup.subtree_control").write_text(
            " ".join("+" + name for name in owner["controllers"]))
        commands = sysfs_commands(parent_mount_namespace,
                                  os.stat("/proc/self/ns/mnt").st_ino,
                                  Path("/proc/self/mountinfo").read_text())
        for command in commands:
            subprocess.run(command, check=True, timeout=10)
        self.validate_inside(owner)
        record = {**owner, "pid": os.getpid(), "start": process_start(os.getpid()),
                  "namespaces": {name: os.stat("/proc/self/ns/" + name).st_ino
                                 for name in ("mnt", "net", "cgroup")}, "ready": True}
        private_json(self.ready_path, record)
        stopped = False

        def stop(_signal, _frame):
            nonlocal stopped
            stopped = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        while not stopped:
            time.sleep(0.2)

    def validate_inside(self, owner: dict) -> None:
        magic = subprocess.check_output(["stat", "-fc", "%t", str(CGROUP_ROOT)], text=True)
        check_view(CGROUP_ROOT.stat().st_ino, owner["cgroup_inode"], magic,
                   Path("/proc/self/cgroup").read_text())
        check_cgroup_mounts(Path("/proc/self/mountinfo").read_text())
        if os.getpid() not in map(int, (CGROUP_ROOT / "control" / "cgroup.procs").read_text().split()):
            raise LabError("runtime cgroup does not contain the executing process")
        available = (CGROUP_ROOT / "cgroup.controllers").read_text()
        enabled = (CGROUP_ROOT / "cgroup.subtree_control").read_text()
        require_controllers(available, enabled)
        config = json.loads((self.root / "mount" / ".config").read_text())
        if config["Format"]["UUID"] != owner["volume_uuid"]:
            raise LabError("runtime-node JuiceFS volume identity mismatch")
        if os.stat("/proc/self/ns/net").st_ino != owner["netns_inode"]:
            raise LabError("runtime-node network identity mismatch")

    def execute(self, command: list[str]) -> None:
        data = self.verified()
        arguments = entered_command(data["pid"], Path(__file__).resolve(), self.root,
                                    self.generation, self.node, command)
        os.execvp(arguments[0], arguments)

    def inner_execute(self, command: list[str]) -> None:
        if not command:
            raise LabError("exec requires a command")
        owner = self.owner_inside()
        (CGROUP_ROOT / "control" / "cgroup.procs").write_text(str(os.getpid()))
        self.validate_inside(owner)
        os.execvp(command[0], command)

    def owner_inside(self) -> dict:
        # The host-absolute cgroup path is deliberately not reachable through this view.
        owner = json.loads(self.owner_path.read_text())
        ready = json.loads(self.ready_path.read_text())
        if (owner.get("task") != self.root.name or owner.get("generation") != self.generation
                or owner.get("node") != self.node or owner.get("cgroup") != str(self.cgroup)
                or owner.get("nonce") != ready.get("nonce")):
            raise LabError("inner runtime-node identity mismatch")
        for name in ("mnt", "net", "cgroup"):
            if os.stat("/proc/self/ns/" + name).st_ino != ready["namespaces"][name]:
                raise LabError("inner runtime-node namespace mismatch")
        return owner

    def stop(self) -> dict:
        data = self.verified()
        processes = set()
        for path in self.cgroup.rglob("cgroup.procs"):
            processes.update(int(value) for value in path.read_text().split())
        if processes - {data["pid"]}:
            raise LabError("runtime-node still has readers/services; refusing teardown")
        os.kill(data["pid"], signal.SIGTERM)
        deadline = time.monotonic() + 10
        while live(data["pid"], data["start"]) and time.monotonic() < deadline:
            time.sleep(0.1)
        if live(data["pid"], data["start"]):
            raise LabError("keeper exit not confirmed; cgroup retained")
        for directory in sorted((p for p in self.cgroup.rglob("*") if p.is_dir()),
                                key=lambda p: len(p.parts), reverse=True):
            directory.rmdir()
        self.cgroup.rmdir()
        return {"node": self.node, "result": "keeper stopped and own empty cgroup removed"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--generation", required=True)
    parser.add_argument("--node", choices=("source", "target"), required=True)
    parser.add_argument("--volume-uuid")
    parser.add_argument("action", choices=("setup", "status", "exec", "stop", "_keeper", "_exec"))
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise LabError("run as root on the independent test host")
    runtime = RuntimeNodes(checked_root(args.root), args.generation, args.node)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if args.action == "setup":
        if not args.volume_uuid:
            raise LabError("setup requires --volume-uuid")
        print(json.dumps(runtime.setup(args.volume_uuid), indent=2))
    elif args.action == "status":
        print(json.dumps(runtime.verified(), indent=2))
    elif args.action == "stop":
        print(json.dumps(runtime.stop(), indent=2))
    elif args.action == "exec":
        runtime.execute(command)
    elif args.action == "_exec":
        runtime.inner_execute(command)
    else:
        if len(command) != 1:
            raise LabError("keeper requires its task nonce")
        runtime.keeper(command[0])


if __name__ == "__main__":
    try:
        main()
    except (LabError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print("runtime-node error: " + str(error), file=sys.stderr)
        sys.exit(1)
