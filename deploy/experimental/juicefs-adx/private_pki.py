"""Task-local TLS material; never installs a system trust anchor."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

from lab import LabError


def ensure_pki(directory: Path, task: str) -> dict[str, Path]:
    paths = {"ca": directory / "ca.pem", "certificate": directory / "ingress.pem",
             "key": directory / "ingress.key"}
    if directory.is_symlink():
        raise LabError("task PKI directory cannot be a symlink")
    if directory.exists():
        if directory.stat().st_mode & 0o077:
            raise LabError("task PKI directory must be private")
        for path in paths.values():
            if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
                raise LabError("existing task PKI is incomplete or not private; preserved")
        _verify(paths, directory)
        return paths
    directory.mkdir(mode=0o700)
    ca_key = directory / "ca.key"
    request = directory / "ingress.csr"
    extension = directory / "ingress.ext"
    old_umask = os.umask(0o077)
    try:
        extension.write_text(
            "basicConstraints=critical,CA:FALSE\n"
            "keyUsage=critical,digitalSignature,keyEncipherment\n"
            "extendedKeyUsage=serverAuth\n"
            "subjectAltName=IP:10.243.251.2,IP:10.243.251.3,IP:127.0.0.1,DNS:localhost\n")
        _run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256",
              "-days", "3", "-subj", "/CN=" + task + "-ca", "-keyout", str(ca_key),
              "-out", str(paths["ca"]), "-addext", "basicConstraints=critical,CA:TRUE",
              "-addext", "keyUsage=critical,keyCertSign,cRLSign"], directory)
        _run(["openssl", "req", "-new", "-newkey", "rsa:2048", "-nodes", "-sha256",
              "-subj", "/CN=a0-source", "-keyout", str(paths["key"]),
              "-out", str(request)], directory)
        _run(["openssl", "x509", "-req", "-in", str(request), "-CA", str(paths["ca"]),
              "-CAkey", str(ca_key), "-CAcreateserial", "-days", "2", "-sha256",
              "-extfile", str(extension), "-out", str(paths["certificate"])], directory)
        _verify(paths, directory)
    finally:
        os.umask(old_umask)
    return paths


def _run(command: list[str], directory: Path) -> None:
    log = directory / "openssl.log"
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "ab") as stream:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=stream,
                                stderr=subprocess.STDOUT, timeout=30)
    if result.returncode:
        raise LabError("task PKI command failed; private log: " + str(log))


def _verify(paths: dict[str, Path], directory: Path) -> None:
    _run(["openssl", "verify", "-CAfile", str(paths["ca"]), "-purpose", "sslserver",
          str(paths["certificate"])], directory)
    _run(["openssl", "x509", "-in", str(paths["certificate"]), "-noout",
          "-checkip", "10.243.251.2"], directory)
