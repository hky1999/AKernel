"""Node runtime broker for Sandboxd-authorized per-environment collections."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from image_provider import ImageProvider

logger = logging.getLogger(__name__)


class RuntimeCollections:
    def __init__(self, root: Path, image_socket: str):
        self.root = root
        self.image_socket = image_socket
        self.providers: dict[str, ImageProvider] = {}
        self.lock = threading.Lock()

    def authorize(self, authorization: str) -> ImageProvider:
        if not authorization.startswith("Bearer "):
            raise PermissionError("Runtime capability required")
        token = authorization.removeprefix("Bearer ")
        collection, _, _secret = token.partition(".")
        if not re.fullmatch(r"[a-f0-9]{32}", collection):
            raise PermissionError("Invalid runtime capability")
        path = self.root / "records" / (collection + ".json")
        try:
            if path.stat().st_mode & 0o077:
                raise PermissionError("Unsafe runtime authorization record")
            record = json.loads(path.read_text())
        except FileNotFoundError as error:
            raise PermissionError("Runtime capability unavailable") from error
        if not hmac.compare_digest(
            hashlib.sha256(token.encode()).hexdigest(), record["token_sha256"]
        ):
            raise PermissionError("Invalid runtime capability")
        if record["collection_id"] != collection:
            raise PermissionError("Runtime collection identity mismatch")
        with self.lock:
            if collection not in self.providers:
                self.providers[collection] = ImageProvider(
                    self.root / "collections" / collection,
                    record["registry"],
                    image_socket=self.image_socket,
                    repository_prefix=record["repository_prefix"],
                    scheme=record["scheme"],
                )
            return self.providers[collection]


def serve(collections: RuntimeCollections, port: int) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/prepare":
                self.send_error(404)
                return
            try:
                provider = collections.authorize(self.headers.get("Authorization", ""))
            except PermissionError:
                self.send_error(401)
                return
            try:
                self.connection.settimeout(180)
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 16 * 1024:
                    raise ValueError("Invalid request size")
                body = json.loads(self.rfile.read(size))
                if set(body) != {"reference"} or not isinstance(body["reference"], str):
                    raise ValueError("Immutable artifact reference required")
                result = provider.prepare(body["reference"])
                payload = json.dumps(result).encode()
            except ValueError as error:
                self.send_error(400, str(error))
                return
            except Exception:
                logger.exception(
                    "Managed image preparation failed; inspect the node runtime"
                )
                self.send_error(500, "Runtime image preparation failed")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--image-socket", default="/run/sandboxd/image-api/image.sock")
    parser.add_argument("--port", type=int, default=8840)
    args = parser.parse_args()
    serve(RuntimeCollections(args.root, args.image_socket), args.port)
