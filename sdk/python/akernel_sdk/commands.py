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

"""Backend-neutral command facade."""

from __future__ import annotations

import codecs
import threading
import time
from collections.abc import Callable
from typing import Any, Literal, overload

from ._backends.base import CommandsDriver
from ._backends.errors import CommandExitException, TimeoutException
from .types import CommandInfo, CommandResult


class CommandHandle:
    """Handle for a background process running in the sandbox."""

    def __init__(
        self,
        pid: int,
        driver: CommandsDriver,
        *,
        on_stdout: Callable[[str], None] | None = None,
        on_stderr: Callable[[str], None] | None = None,
    ) -> None:
        self.pid = pid
        self._driver = driver
        self._on_stdout = on_stdout
        self._on_stderr = on_stderr
        self._stdout_offset = 0
        self._stderr_offset = 0
        self._stdout_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._stderr_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._disconnected = threading.Event()

    @property
    def command_id(self) -> str | None:
        """Stable backend command ID, suitable for reconnecting from another client."""
        value = self._driver.command_id(self.pid)
        return value if isinstance(value, str) else None

    def wait(
        self,
        timeout: int | None = None,
        *,
        check: bool = False,
        on_stdout: Callable[[str], None] | None = None,
        on_stderr: Callable[[str], None] | None = None,
    ) -> CommandResult:
        """Wait for completion; the default backend returns a timeout result."""

        if not isinstance(check, bool):
            raise TypeError("check must be a boolean")
        stdout = self._on_stdout if on_stdout is None else on_stdout
        stderr = self._on_stderr if on_stderr is None else on_stderr
        _validate_callbacks(stdout, stderr)
        if self._disconnected.is_set():
            raise RuntimeError("command handle disconnected; use commands.connect()")
        if stdout is not None or stderr is not None:
            self._driver.require_output()
            self._observe_output(timeout, stdout, stderr)
            result = self._driver.wait(self.pid, 0)
        else:
            result = self._driver.wait(self.pid, timeout)
        return _checked_result(result, check)

    def _observe_output(
        self,
        timeout: int | None,
        stdout: Callable[[str], None] | None,
        stderr: Callable[[str], None] | None,
    ) -> None:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self._disconnected.is_set():
                raise RuntimeError(
                    "command handle disconnected; command was not killed"
                )
            chunk = self._driver.output(
                self.pid, self._stdout_offset, self._stderr_offset
            )
            terminal = chunk.terminal and not chunk.has_more
            out = self._stdout_decoder.decode(chunk.stdout, final=terminal)
            err = self._stderr_decoder.decode(chunk.stderr, final=terminal)
            self._stdout_offset, self._stderr_offset = (
                chunk.stdout_offset,
                chunk.stderr_offset,
            )
            if out and stdout is not None:
                stdout(out)
            if err and stderr is not None:
                stderr(err)
            if terminal:
                return
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutException(
                    "command output wait timed out; command may still run",
                    code="WAIT_TIMEOUT",
                    command_id=self.command_id,
                )
            self._disconnected.wait(0.05)

    def disconnect(self) -> None:
        """Stop local output observation; leave the remote command running."""
        self._disconnected.set()

    def kill(self) -> bool:
        """Return whether a live process was signalled."""

        return self._driver.kill(self.pid)

    def send_stdin(self, data: str, eof: bool = False) -> None:
        """Write text to the process's stdin."""

        self._driver.send_stdin(self.pid, data, eof)

    def close_stdin(self) -> None:
        """Close the process's stdin so it observes EOF."""

        self.send_stdin("", eof=True)


class Commands:
    """Client-side wrapper for command execution on the remote sandbox."""

    def __init__(self, driver: CommandsDriver) -> None:
        self._driver = driver

    @overload
    def run(
        self,
        cmd: str,
        background: Literal[False] = False,
        envs: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout: int = 60,
        stdin: Literal[False] = False,
        *,
        check: bool = False,
        on_stdout: Callable[[str], None] | None = None,
        on_stderr: Callable[[str], None] | None = None,
        user: str | None = None,
    ) -> CommandResult: ...

    @overload
    def run(
        self,
        cmd: str,
        background: Literal[True],
        envs: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout: int = 60,
        stdin: bool = False,
        *,
        check: bool = False,
        on_stdout: Callable[[str], None] | None = None,
        on_stderr: Callable[[str], None] | None = None,
        user: str | None = None,
    ) -> CommandHandle: ...

    def run(
        self,
        cmd: str,
        background: bool = False,
        envs: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout: int = 60,
        stdin: bool = False,
        *,
        check: bool = False,
        on_stdout: Callable[[str], None] | None = None,
        on_stderr: Callable[[str], None] | None = None,
        user: str | None = None,
    ) -> CommandResult | CommandHandle:
        """Execute a foreground command or start a background process.

        An explicit ``cwd`` must already exist inside the sandbox. When omitted,
        commands use the default directory set by ``Sandbox(cwd=...)``.
        """

        if not isinstance(check, bool):
            raise TypeError("check must be a boolean")
        _validate_callbacks(on_stdout, on_stderr)
        if user is not None and (not isinstance(user, str) or not user.strip()):
            raise ValueError("user must be a non-empty username or UID string")
        streaming = on_stdout is not None or on_stderr is not None
        if streaming:
            self._driver.require_output()
        if stdin and not background:
            raise ValueError("stdin=True requires background=True")
        if background or streaming:
            options: dict[str, Any] = {}
            if user is not None:
                options["user"] = user
            if streaming and not background:
                options["execution_timeout"] = timeout
            pid = self._driver.start(
                cmd,
                envs=envs,
                cwd=cwd,
                stdin=stdin,
                **options,
            )
            handle = CommandHandle(
                pid, self._driver, on_stdout=on_stdout, on_stderr=on_stderr
            )
            if background:
                return handle
            try:
                return handle.wait(timeout=timeout, check=check)
            except BaseException as error:
                context: Any = error
                context.command_id = handle.command_id
                context.pid = handle.pid
                raise
        if user is not None:
            return _checked_result(
                self._driver.run(cmd, envs=envs, cwd=cwd, timeout=timeout, user=user),
                check,
            )
        return _checked_result(
            self._driver.run(cmd, envs=envs, cwd=cwd, timeout=timeout), check
        )

    def connect(
        self,
        command: str | int,
        *,
        on_stdout: Callable[[str], None] | None = None,
        on_stderr: Callable[[str], None] | None = None,
    ) -> CommandHandle:
        """Reconnect to a tracked command by stable ID or PID without rerunning it."""
        if isinstance(command, bool) or not isinstance(command, (str, int)):
            raise TypeError("command must be a stable ID or positive PID")
        if (isinstance(command, str) and not command.strip()) or (
            isinstance(command, int) and command <= 0
        ):
            raise ValueError("command must be a non-empty ID or positive PID")
        _validate_callbacks(on_stdout, on_stderr)
        if on_stdout is not None or on_stderr is not None:
            self._driver.require_output()
        return CommandHandle(
            self._driver.connect(command),
            self._driver,
            on_stdout=on_stdout,
            on_stderr=on_stderr,
        )

    def list(self) -> list[CommandInfo]:
        """Return snapshots of processes tracked by this sandbox."""

        return self._driver.list()

    def kill(self, pid: int) -> bool:
        """Return whether a live process was signalled."""

        return self._driver.kill(pid)

    def send_stdin(self, pid: int, data: str, eof: bool = False) -> None:
        """Write text to the stdin of a tracked process."""

        self._driver.send_stdin(pid, data, eof)

    def close_stdin(self, pid: int) -> None:
        """Close the stdin of a tracked process."""

        self.send_stdin(pid, "", eof=True)


def _checked_result(result: CommandResult, check: bool) -> CommandResult:
    if not isinstance(check, bool):
        raise TypeError("check must be a boolean")
    if check:
        if result.exit_code is None:
            raise TimeoutException(
                result.error_message
                or "command wait timed out; the command may still run",
                code=result.error_code or "WAIT_TIMEOUT",
            )
        if result.exit_code != 0:
            raise CommandExitException(result.stdout, result.stderr, result.exit_code)
    return result


def _validate_callbacks(*callbacks: Callable[[str], None] | None) -> None:
    if any(callback is not None and not callable(callback) for callback in callbacks):
        raise TypeError("output callbacks must be callable or None")
