"""Startup-declared, immutable Compose images mounted by AKernel.

The snapshotter and guest bootstrap originate from tianyuzhou95's image-bridge
POC, agent-worker commit 9b3434d36086d920b6bb7f3322da642381a254b8.
Docker keeps ownership of container lifecycle; this module stages the mount plan.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable


def _digest(value: Any) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None
    ):
        raise ValueError("Image bridge requires sha256 image and metadata digests")
    return value


@dataclass(frozen=True)
class ImageMountPlan:
    directory: Path
    images: list[dict[str, Any]]
    services: dict[str, str]
    aliases: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path, *, bases_only: bool = False) -> ImageMountPlan:
        path = Path(path).resolve()
        data = json.loads(path.read_text())
        images, services = data.get("images"), data.get("services")
        if not isinstance(images, list) or (not images and not bases_only):
            raise ValueError("Image plan requires a nonempty images list")
        if not isinstance(services, dict) or (
            "main" not in services and not bases_only
        ):
            raise ValueError("Image plan requires a services map including main")
        by_reference = {}
        for image in images:
            reference = image["reference"]
            digest = _digest(image["target"]["digest"])
            if (
                not reference.endswith("@" + digest)
                or image["root"] != "/opt/akernel-images/" + digest[7:]
                or reference in by_reference
            ):
                raise ValueError("Image reference, digest and immutable mount disagree")
            _digest(image["chain_id"])
            by_reference[reference] = image
            descriptors = set()
            for blob in image["blobs"]:
                name = blob["file"]
                if not isinstance(name, str) or Path(name).name != name:
                    raise ValueError("Image metadata must be a sibling filename")
                metadata = path.parent / name
                if metadata.is_symlink():
                    raise ValueError("Image metadata must not be a symlink")
                payload = metadata.read_bytes()
                descriptor = blob["descriptor"]
                actual = "sha256:" + hashlib.sha256(payload).hexdigest()
                if (
                    actual != _digest(descriptor["digest"])
                    or len(payload) != descriptor["size"]
                ):
                    raise ValueError("Image metadata digest or size mismatch")
                descriptors.add(actual)
            if not {digest, _digest(image["config"]["digest"])} <= descriptors:
                raise ValueError("Image plan is missing its manifest or config")
        for service, reference in services.items():
            if (
                not isinstance(service, str)
                or not service
                or reference not in by_reference
            ):
                raise ValueError(
                    "Every planned service must reference a declared image"
                )
        aliases = data.get("aliases", {})
        if not isinstance(aliases, dict) or any(
            not isinstance(alias, str) or not alias or reference not in by_reference
            for alias, reference in aliases.items()
        ):
            raise ValueError("Image aliases must reference declared immutable images")
        selected = (
            list(by_reference) if bases_only else list(dict.fromkeys(services.values()))
        )
        return cls(
            path.parent, [by_reference[r] for r in selected], dict(services), aliases
        )

    def mounts(self, factory: Callable[..., Any]) -> list[Any]:
        # Different registry names can refer to the same immutable image root.
        by_root = {i["root"]: i for i in self.images}
        return [
            factory(target=i["root"], image_url=i["reference"])
            for i in by_root.values()
        ]
