#!/usr/bin/env python3
"""为已构建镜像生成并执行隔离开发容器的启动计划。"""

import argparse
import json
from pathlib import Path
import socket
import subprocess

NAME = "akernel-agent-dx-dev-node"
NETWORK = "akernel-agent-dx-dev"
OWNER = "akernel.scheduler.environment=agent-dx-dev-20261010"
STATE = Path("/data/akernel-agent-dx-dev/state")


def commands(image, *, elastic_memory=False, history_reclamation=False, pressure_source=None):
    environment = ["--env", "AKERNEL_SCHEDULER_ELASTIC_MEMORY=true"] if elastic_memory else []
    if history_reclamation:
        environment += ["--env", "AKERNEL_SCHEDULER_HISTORY_RECLAMATION=true"]
    if pressure_source is not None:
        path = Path(pressure_source)
        if not path.is_absolute() or path.is_symlink() or not path.is_file() or path.stat().st_size > 8192:
            raise ValueError("压力来源要求有界的绝对真实文件")
        source = json.loads(path.read_text())
        observed = Path(source["path"])
        if (set(source) != {"path", "identity", "max_age_millis"}
                or observed.parent != Path("/run/akernel-pressure")
                or observed.name == path.name or source["identity"].get("host_id") != "BareMetal1"
                or type(source["max_age_millis"]) is not int or not 100 <= source["max_age_millis"] <= 60000):
            raise ValueError("压力来源的宿主、路径或有效期无效")
        environment += ["--mount", f"type=bind,source={path.parent},target=/run/akernel-pressure,readonly",
                        "--env", "AKERNEL_SCHEDULER_PRESSURE_SOURCE=/run/akernel-pressure/" + path.name]
    return [
        ["docker", "network", "create", "--label", OWNER, NETWORK],
        ["docker", "run", "--detach", "--name", NAME, "--label", OWNER,
         "--network", NETWORK, "--privileged", "--cgroupns", "private",
         "--cpus", "4", "--cpuset-cpus", "0-7", "--memory", "16g",
         "--memory-swap", "16g", "--pids-limit", "8192",
         "--publish", "127.0.0.1:19443:8443", "--publish", "127.0.0.1:19080:8080",
         "--mount", f"type=bind,source={STATE},target=/home/akernel",
         "--log-opt", "max-size=16m", "--log-opt", "max-file=3", *environment, image],
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--elastic-memory", action="store_true",
                        help="启用匹配候选的自动内存策略和 75%% 的软节流比例")
    parser.add_argument("--history-reclamation", action="store_true",
                        help="启用匹配组件的持久历史回收边界")
    parser.add_argument("--pressure-source", type=Path, help="启用匹配的宿主压力观测来源，默认关闭")
    args = parser.parse_args()
    # 镜像必须使用完整本地内容身份，不通过可变标签启动。
    if not args.image.startswith("sha256:") or len(args.image) != 71:
        parser.error("--image 必须是完整 sha256 镜像身份")
    try:
        int(args.image[7:], 16)
    except ValueError:
        parser.error("镜像 SHA256 非法")
    plan = commands(args.image, elastic_memory=args.elastic_memory,
                    history_reclamation=args.history_reclamation, pressure_source=args.pressure_source)
    print(json.dumps({"state": str(STATE), "commands": plan}, indent=2), flush=True)
    if not args.apply:
        return
    for kind, name in (("container", NAME), ("network", NETWORK)):
        result = subprocess.run(["docker", kind, "inspect", name], capture_output=True)
        if result.returncode == 0:
            raise RuntimeError(f"{kind} {name} 已存在，请先核对其物理归属")
    subprocess.run(["docker", "image", "inspect", args.image], check=True,
                   stdout=subprocess.DEVNULL)
    for port in (19443, 19080):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", port))
    STATE.mkdir(parents=True, exist_ok=True)
    for command in plan:
        subprocess.run(command, check=True)


if __name__ == "__main__":
    main()
