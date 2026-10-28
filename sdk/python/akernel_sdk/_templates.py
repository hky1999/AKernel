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

"""Tenant-scoped immutable template registrations, separate from snapshot labels."""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable

from ._backends.base import Backend
from ._backends.registry import load_backend
from ._snapshots import snapshot_id as _snapshot_id
from .types import SnapshotInfo, TemplateInfo


def _template_id(value: str | TemplateInfo) -> str:
    identity = value.template_id if isinstance(value, TemplateInfo) else value
    if not isinstance(identity, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,128}", identity
    ):
        raise ValueError("template must be a TemplateInfo or an opaque template ID")
    return identity


class TemplatePaginator:
    """Lazy pages; failed requests leave the cursor intact and are not retried."""

    def __init__(
        self,
        fetch: Callable[[str | None], tuple[list[TemplateInfo], str | None]],
        next_token: str | None,
    ):
        self._fetch = fetch
        self._next_token = next_token
        self._has_next = True

    @property
    def has_next(self) -> bool:
        return self._has_next

    @property
    def next_token(self) -> str | None:
        return self._next_token

    def next_items(self) -> list[TemplateInfo]:
        if not self._has_next:
            return []
        items, token = self._fetch(self._next_token)
        if token and token == self._next_token:
            raise RuntimeError("template listing returned a non-advancing cursor")
        self._next_token = token or None
        self._has_next = bool(token)
        return items


class Template:
    """Catalog of initialized reusable environments on the configured cluster.

    Requires the template-capable service. Names are display labels, not aliases.
    No image build/upload is performed by this MVP.
    """

    _bound_backend: Backend | None = None

    @classmethod
    def _backend(cls) -> Backend:
        return cls._bound_backend if cls._bound_backend is not None else load_backend()

    @classmethod
    def import_snapshot(
        cls,
        snapshot_id: str | SnapshotInfo,
        *,
        name: str,
        template_id: str | None = None,
    ) -> TemplateInfo:
        """Register a ready snapshot and atomically pin it for this template.

        The snapshot remains caller-owned. An optional stable template_id allows
        reconciliation after an uncertain response; reusing it with different
        content or after deletion is rejected. Errors retain template_id.
        """
        source = _snapshot_id(snapshot_id)
        if (
            not isinstance(name, str)
            or not name.strip()
            or len(name.encode()) > 256
            or any(ord(c) < 32 or ord(c) == 127 for c in name)
        ):
            raise ValueError(
                "name must be a non-empty display label of at most 256 bytes"
            )
        identity = _template_id(
            f"tpl-{uuid.uuid4()}" if template_id is None else template_id
        )
        return cls._backend().import_template(source, name.strip(), identity)

    @classmethod
    def get(cls, template: str | TemplateInfo) -> TemplateInfo:
        """Get a registered template by ID; deleted/foreign items are not found."""
        return cls._backend().get_template(_template_id(template))

    @classmethod
    def list(
        cls, *, name: str | None = None, limit: int = 100, next_token: str | None = None
    ) -> TemplatePaginator:
        """List this tenant's ready registrations, with exact name filtering."""
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 1000
        ):
            raise ValueError("limit must be between 1 and 1000")
        if name is not None and (not isinstance(name, str) or not name.strip()):
            raise ValueError("name must be a non-empty string or None")
        if next_token is not None and not isinstance(next_token, str):
            raise TypeError("next_token must be a string or None")
        backend = cls._backend()
        return TemplatePaginator(
            lambda token: backend.list_templates(name, token, limit), next_token
        )

    @classmethod
    def delete(cls, template: str | TemplateInfo) -> None:
        """Unregister and release its pin; preserve the snapshot and running children.

        Repeated deletion is idempotent. This does not kill sandboxes or call
        delete_snapshot. Explicitly delete the snapshot after its other users end.
        """
        cls._backend().delete_template(_template_id(template))
