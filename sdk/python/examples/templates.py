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

"""Template registration/restore. Run with --image <image> on a capable service."""

import argparse
import sys
import uuid

from akernel_sdk import Sandbox, Template


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    snapshot = None
    failed = False
    identity = "tpl-" + uuid.uuid4().hex
    try:
        with Sandbox(image=args.image) as source:
            source.files.write("/tmp/template-example", "initialized")
            snapshot = source.create_snapshot()
        info = Template.import_snapshot(
            snapshot, name="sdk-template-example", template_id=identity
        )
        assert Template.get(info) == info
        with Sandbox.create(template=info) as first:
            with Sandbox.from_template(info) as second:
                first.files.write("/tmp/template-example", "private")
                assert second.files.read("/tmp/template-example") == "initialized"
                Template.delete(info)
                assert first.commands.run("printf alive").stdout == "alive"
        print("Template restored independent children; unregister preserved them")
    except BaseException:
        failed = True
        print(
            f"Inspect template {identity} and snapshot {snapshot} "
            "before retrying cleanup; "
            "the exception retains any known child identities.",
            file=sys.stderr,
        )
        raise
    finally:
        if not failed:
            Template.delete(identity)
        if not failed and snapshot is not None:
            Sandbox.delete_snapshot(snapshot)


if __name__ == "__main__":
    main()
