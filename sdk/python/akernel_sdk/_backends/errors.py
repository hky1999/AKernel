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

"""Stable errors raised at the AKernel backend boundary."""

from typing import Any


class InvalidBackendError(ValueError):
    """The configured backend identifier is not supported."""


class BackendNotInstalledError(ImportError):
    """The selected backend dependency is not installed."""


class UnsupportedBackendFeatureError(ValueError):
    """The selected backend cannot preserve a requested AKernel feature."""


class BackendOperationError(RuntimeError):
    """A backend operation failed, with optional service error context.

    Missing fields are ``None``. ``retry`` and ``outcome`` retain the service's
    instructions; callers must not infer execution state from the message or
    an HTTP status. The original backend error remains available as the cause.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        retry: str | None = None,
        outcome: str | None = None,
        request_id: str | None = None,
        operation_id: str | None = None,
        environment_id: str | None = None,
        status_code: int | None = None,
        scheduling_failure: dict[str, Any] | None = None,
        failure: dict[str, Any] | None = None,
        probe_failure: dict[str, Any] | None = None,
        request_forwarded: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retry = retry
        self.outcome = outcome
        self.request_id = request_id
        self.operation_id = operation_id
        self.environment_id = environment_id
        self.status_code = status_code
        self.scheduling_failure = scheduling_failure
        self.failure = failure
        self.probe_failure = probe_failure
        self.request_forwarded = request_forwarded
