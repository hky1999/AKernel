#!/usr/bin/env python3
"""Task-owned L2 network for two native ADX nodes; never changes host NAT."""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys

from lab import Lab, LabError, checked_root, private_json


def route_collision(routes: list[dict], subnet: str) -> bool:
    wanted = ipaddress.ip_network(subnet)
    for route in routes:
        destination = route.get("dst", "default")
        if destination == "default":
            continue
        network = ipaddress.ip_network(destination, strict=False)
        if network.version == wanted.version and network.overlaps(wanted):
            return True
    return False


def link_owned(link: dict, name: str, task: str) -> bool:
    return link.get("ifname") == name and link.get("ifalias") == task


def namespace_matches(path: Path, record: dict) -> bool:
    try:
        stat = path.stat()
    except OSError:
        return False
    return stat.st_dev == record.get("dev") and stat.st_ino == record.get("ino")


class Network:
    def __init__(self, root: Path):
        self.lab = Lab(root)
        if self.lab.state.get("task") != root.name:
            raise LabError("initialized task identity required")
        self.root = root
        self.file = root / "node-network.json"
        self.state = json.loads(self.file.read_text()) if self.file.exists() else {}
        if self.state and self.state.get("task") != root.name:
            raise LabError("network task identity mismatch")

    def run(self, args: list[str]) -> str:
        return self.lab.run(args, "node-network", timeout=15)

    def save(self) -> None:
        private_json(self.file, self.state)

    def owned_link(self, name: str) -> None:
        data = json.loads(self.run(["ip", "-j", "link", "show", "dev", name]))
        if len(data) != 1 or not link_owned(data[0], name, self.root.name):
            raise LabError("network interface ownership mismatch; refusing mutation")

    def up(self) -> None:
        if self.state:
            raise LabError("network already recorded; inspect before replacing")
        subnet = "10.243.251.0/24"
        if route_collision(json.loads(self.run(["ip", "-j", "route", "show"])), subnet):
            raise LabError("task network overlaps an existing host route")
        suffix = self.root.name.rsplit("-", 1)[-1].lower()[:6]
        bridge = "a0b" + suffix
        nodes = {
            node: {"name": "a0n-" + suffix + "-" + short,
                   "host_link": "a0" + short + suffix,
                   "peer_link": "a0" + short + "p" + suffix,
                   "address": "10.243.251." + address}
            for node, short, address in (("source", "s", "2"), ("target", "t", "3"))
        }
        existing_links = {item["ifname"] for item in json.loads(self.run(["ip", "-j", "link", "show"]))}
        existing_names = {line.split()[0] for line in self.run(["ip", "netns", "list"]).splitlines() if line}
        if bridge in existing_links or any(
            item["host_link"] in existing_links or item["peer_link"] in existing_links
            or item["name"] in existing_names for item in nodes.values()
        ):
            raise LabError("task network name is already occupied")
        self.state = {"task": self.root.name, "subnet": subnet,
                      "bridge": bridge, "nodes": {}, "ready": False}
        self.save()
        self.run(["ip", "link", "add", bridge, "type", "bridge"])
        self.run(["ip", "link", "set", "dev", bridge, "alias", self.root.name])
        self.state["bridge_created"] = True
        self.save()
        self.run(["ip", "addr", "add", "10.243.251.1/24", "dev", bridge])
        self.run(["ip", "link", "set", bridge, "up"])
        for node, record in nodes.items():
            name = record["name"]
            self.run(["ip", "netns", "add", name])
            stat = Path("/run/netns", name).stat()
            record.update({"dev": stat.st_dev, "ino": stat.st_ino})
            self.state["nodes"][node] = record
            self.save()
            host, peer = record["host_link"], record["peer_link"]
            self.run(["ip", "link", "add", host, "type", "veth", "peer", "name", peer])
            self.run(["ip", "link", "set", "dev", host, "alias", self.root.name])
            self.run(["ip", "link", "set", "dev", peer, "alias", self.root.name])
            self.run(["ip", "link", "set", peer, "netns", name])
            self.run(["ip", "link", "set", host, "master", bridge])
            self.run(["ip", "link", "set", host, "up"])
            self.run(["ip", "-n", name, "link", "set", "lo", "up"])
            self.run(["ip", "-n", name, "link", "set", peer, "name", "uplink0"])
            self.run(["ip", "-n", name, "addr", "add", record["address"] + "/24", "dev", "uplink0"])
            self.run(["ip", "-n", name, "link", "set", "uplink0", "up"])
            self.run(["ip", "-n", name, "route", "add", "default", "via", "10.243.251.1"])
        self.state["ready"] = True
        self.save()
        print(json.dumps(self.state, indent=2))

    def down(self) -> None:
        if not self.state:
            return
        for record in self.state["nodes"].values():
            if not namespace_matches(Path("/run/netns", record["name"]), record):
                raise LabError("network namespace identity mismatch; resources retained")
            if self.run(["ip", "netns", "pids", record["name"]]).strip():
                raise LabError("node still has live processes; stop owned services before network cleanup")
            self.owned_link(record["host_link"])
        if self.state.get("bridge_created"):
            self.owned_link(self.state["bridge"])
            members = json.loads(self.run(["ip", "-j", "link", "show", "master", self.state["bridge"]]))
            expected = {record["host_link"] for record in self.state["nodes"].values()}
            if any(item.get("ifname") not in expected or item.get("ifalias") != self.root.name for item in members):
                raise LabError("foreign bridge member found; resources retained")
        for record in self.state["nodes"].values():
            self.run(["ip", "netns", "delete", record["name"]])
        if self.state.get("bridge_created"):
            self.run(["ip", "link", "delete", self.state["bridge"]])
        self.file.unlink()
        self.state = {}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("action", choices=("up", "down"))
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise LabError("network setup requires root on the test host")
    network = Network(checked_root(args.root))
    getattr(network, args.action)()


if __name__ == "__main__":
    try:
        main()
    except (LabError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print("node network error: " + str(error), file=sys.stderr)
        sys.exit(1)
