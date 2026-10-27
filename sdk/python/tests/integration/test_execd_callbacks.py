"""Real SDK/Execd HTTP interop, optionally inside a separately owned sandbox.

Set AKERNEL_TEST_EXECD_URL and AKERNEL_TEST_EXECD_TOKEN. This tests command
behavior only, not deployment of the control-plane lifetime/metadata extension.
"""

import os
import threading
import unittest
import uuid

import httpx
from adx_environment.commands import Commands as NativeCommands

from akernel_sdk import CommandExitException
from akernel_sdk._backends.adx import _CommandsDriver
from akernel_sdk.commands import Commands


class DirectExecd:
    """Test transport for the real Execd /invoke endpoint, with no mocked actions."""

    def __init__(self):
        self._connection = None  # Native waits use actual HTTP polling.
        self.starts = 0
        self.client = httpx.Client(
            base_url=os.environ["AKERNEL_TEST_EXECD_URL"].rstrip("/") + "/",
            headers={
                "X-Auth": os.environ["AKERNEL_TEST_EXECD_TOKEN"],
                "Authorization": "Bearer " + os.environ.get("AKERNEL_TOKEN", ""),
            },
            verify=os.environ.get("SSL_CERT_FILE", True),
            trust_env=False,
        )

    def invoke(self, _identity, action, args=None, *, timeout=None):
        if action == "process.start":
            self.starts += 1
        response = self.client.post(
            "invoke",
            json={
                "action": action,
                "args": args or {},
                "requestId": "invoke-" + str(uuid.uuid4()),
            },
            timeout=60 if timeout is None else (None if timeout < 0 else timeout),
        )
        response.raise_for_status()
        return response.json()


@unittest.skipUnless(
    os.environ.get("AKERNEL_TEST_EXECD_URL"),
    "provide an isolated candidate Execd endpoint",
)
class ExecdCallbacksIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.transport = DirectExecd()
        self.addCleanup(self.transport.client.close)
        self.commands = Commands(
            _CommandsDriver(NativeCommands(self.transport, "owned-candidate"))
        )

    def test_stdout_stderr_arrive_before_exit_and_reconnection_does_not_replay(self):
        handle = self.commands.run(
            "printf 'first'; printf 'error' >&2; read reply; printf '%s' \"$reply\"",
            background=True,
            stdin=True,
        )
        self.addCleanup(handle.kill)
        out, err = [], []
        completed = threading.Event()
        released = threading.Event()
        failed = []

        def stdout(text):
            out.append(text)
            if "first" in "".join(out) and not released.is_set():
                released.set()
                # The command cannot exit until the callback supplies its input.
                handle.send_stdin("continue\n")

        def wait():
            try:
                result = handle.wait(
                    timeout=20, on_stdout=stdout, on_stderr=err.append, check=True
                )
                self.assertEqual(result.stdout, "firstcontinue")
            except BaseException as error:
                failed.append(error)
            finally:
                completed.set()

        thread = threading.Thread(target=wait, daemon=True)
        thread.start()
        self.assertTrue(completed.wait(25), "live callback never released the child")
        thread.join(1)
        if failed:
            raise failed[0]
        self.assertEqual("".join(out), "firstcontinue")
        self.assertEqual("".join(err), "error")
        second = Commands(
            _CommandsDriver(NativeCommands(self.transport, "owned-candidate"))
        )
        result = second.connect(handle.command_id).wait(timeout=5, check=True)
        self.assertEqual(result.stdout, "firstcontinue")
        self.assertEqual(self.transport.starts, 1)

    def test_explicit_user_and_checked_exit_keep_streams(self):
        result = self.commands.run("id -u", user="nobody", check=True)
        self.assertEqual(result.stdout.strip(), "65534")
        with self.assertRaises(CommandExitException) as raised:
            self.commands.run(
                "printf out; printf err >&2; exit 7", user="nobody", check=True
            )
        self.assertEqual(
            (
                raised.exception.stdout,
                raised.exception.stderr,
                raised.exception.exit_code,
            ),
            ("out", "err", 7),
        )
        self.assertEqual(self.transport.starts, 2)


if __name__ == "__main__":
    unittest.main()
