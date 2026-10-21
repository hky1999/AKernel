#!/usr/bin/env python3
"""Read-only admission gate for the task-owned, two-node ADX deployment."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time


class NotReady(RuntimeError):
    pass


def services_ready(status, node):
    if status.get("ok") is not True or not isinstance(status.get("services"), list):
        raise NotReady("invalid supervisor status")
    rows = status["services"]
    required = {"adxlet"}
    if node == "source":
        required.update(("redis", "coordinator", "apiserver"))
    if not required.issubset({row.get("role") for row in rows}):
        raise NotReady("required service is absent")
    for row in rows:
        if (row.get("failed") is not False or type(row.get("pid")) is not int
                or row["pid"] <= 0 or row.get("restarts") != 0):
            raise NotReady("service is stopped, failed, or has restarted")
    return [{key: row[key] for key in ("id", "role", "pid", "restarts")}
            for row in rows]


def empty_page(page):
    return page.get("count") == 0 and page.get("instanceInfos") == []


def admission_ready(page, node):
    admission = page.get("node", {})
    if (admission.get("node_id") != "a0-" + node
            or admission.get("accepting_allocations") is not True
            or admission.get("scheduling_paused") is not False
            or not empty_page(page)):
        raise NotReady("node admission is closed or initial instances are not empty")
    return admission


def process_start(pid):
    raw = (Path("/proc") / str(pid) / "stat").read_text()
    fields = raw[raw.rfind(")") + 2:].split()
    if fields[0] == "Z":
        raise NotReady("service is a zombie")
    return fields[19]


HTTP_PROBE = r'''
import json, pathlib, ssl, sys, urllib.request
root = pathlib.Path(sys.argv[1])
token = (root / "secrets/native-api-key").read_text().strip()
context = ssl.create_default_context(cafile=str(root / "secrets/native-pki/ca.pem"))
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                    urllib.request.HTTPSHandler(context=context))
def get(url):
    request = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    with opener.open(request, timeout=5) as response:
        return json.load(response)
nodes = {node: get("http://127.0.0.1:27492/instance-manager/query-tenant-instances?node_id=a0-" + node)
         for node in ("source", "target")}
queue = get("https://10.243.251.2:27443/global-scheduler/scheduling_queue")
print(json.dumps({"nodes": nodes, "queue": queue}))
'''


def sample(root, generation, expected_sha):
    def prefix(node):
        return [sys.executable, str(root / "profile/runtime_nodes.py"),
                "--root", str(root), "--generation", generation,
                "--node", node, "exec", "--"]

    def run(command):
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=25)
        return json.loads(result.stdout)

    statuses = {}
    for node in ("source", "target"):
        status = run(prefix(node) + [str(root / "package/bin/adxctl"), "status", "--config",
                                   str(root / generation / node / "deployment.yaml")])
        rows = services_ready(status, node)
        for row in rows:
            row["start"] = process_start(row["pid"])
            if row["role"] == "adxlet":
                executable = Path("/proc") / str(row["pid"]) / "exe"
                if executable.resolve() != (root / "bin/adxlet").resolve():
                    raise NotReady("unexpected adxlet executable")
                with executable.open("rb") as stream:
                    if hashlib.file_digest(stream, "sha256").hexdigest() != expected_sha:
                        raise NotReady("unexpected adxlet digest")
        statuses[node] = rows
    http = run(prefix("source") + [sys.executable, "-c", HTTP_PROBE, str(root)])
    admissions = {node: admission_ready(http["nodes"][node], node)
                  for node in ("source", "target")}
    if not empty_page(http["queue"]):
        raise NotReady("initial scheduling queue is not empty")
    return {"services": statuses, "admissions": admissions, "queue": http["queue"],
            "adxlet_sha256": expected_sha}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--generation", required=True)
    parser.add_argument("--adxlet-sha256", required=True)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--stable-seconds", type=float, default=8)
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    output = root / "evidence" / (args.generation + "-admission.json")
    if output.exists():
        parser.error("refusing to replace existing admission evidence")
    deadline = time.monotonic() + args.timeout
    stable_since, previous, result, last_error = None, None, None, "not sampled"
    while time.monotonic() < deadline:
        try:
            result = sample(root, args.generation, args.adxlet_sha256)
            signature = json.dumps(result["services"], sort_keys=True)
            if signature != previous:
                previous, stable_since = signature, time.monotonic()
            if time.monotonic() - stable_since >= args.stable_seconds:
                result.update(ready=True, stable_seconds=time.monotonic() - stable_since)
                break
        except (NotReady, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            previous, stable_since = None, None
            last_error = str(error) if isinstance(error, NotReady) else type(error).__name__
        time.sleep(2)
    else:
        result = {"ready": False, "last_error": last_error}
    with output.open("x", encoding="utf-8") as stream:
        output.chmod(0o600)
        json.dump(result, stream, indent=2)
        stream.write("\n")
    print(json.dumps(result, indent=2))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
