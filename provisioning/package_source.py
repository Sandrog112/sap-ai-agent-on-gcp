"""Assemble what a service is deployed from: its packages and a requirements.txt with the locked shared dependencies
plus its own group. Nothing else in the repository (.env above all) is ever uploaded.

Terraform's `external` data source runs this (terraform/sources.tf): it reads {"service", "packages",
"dependency_group"} as JSON on stdin and prints {"directory", "archive", "sha256"}. The directory is what Cloud Build
builds, the archive is what Agent Runtime takes inline (base64 of a .tar.gz), and the hash names the image and tells
Terraform when the source changed. The archive is reproducible, so an unchanged source gives an unchanged hash.

Only the standard library is used, besides `uv`, which exports the lock file.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD_DIR = ROOT / "build"
REQUIREMENTS_FILE = "requirements.txt"


def assemble(service: str, packages: list[str], dependency_group: str) -> Path:
    if shutil.which("uv") is None:
        sys.exit("uv is required to export the locked requirements: https://docs.astral.sh/uv/")
    directory = BUILD_DIR / service
    shutil.rmtree(directory, ignore_errors=True)
    directory.mkdir(parents=True)
    for package in packages:
        shutil.copytree(ROOT / package, directory / package, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    export = ["uv", "export", "--frozen", "--no-default-groups", "--group", dependency_group]
    export += ["--no-hashes", "--no-emit-project", "-o", str(directory / REQUIREMENTS_FILE)]
    subprocess.run(export, cwd=ROOT, check=True, stdout=subprocess.DEVNULL, stderr=sys.stderr)
    return directory


def archive(directory: Path, target: Path) -> str:
    """Write the directory's files as a .tar.gz with fixed timestamps and owners. Returns the sha256 of the files' names
    and contents, not of the archive, whose bytes also depend on the zlib of the machine that compressed it."""
    buffer = io.BytesIO()
    digest = hashlib.sha256()
    gzip_file = gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0)
    with gzip_file, tarfile.open(fileobj=gzip_file, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for path in sorted(directory.rglob("*")):
            if "__pycache__" in path.parts:
                continue
            info = tar.gettarinfo(str(path), arcname=path.relative_to(directory).as_posix())
            info.mtime, info.uid, info.gid, info.uname, info.gname = 0, 0, 0, "", ""
            info.mode = 0o755 if path.is_dir() else 0o644
            digest.update(info.name.encode() + b"\0")
            if path.is_file():
                digest.update(path.read_bytes())
                with path.open("rb") as file:
                    tar.addfile(info, file)
            else:
                tar.addfile(info)
    target.write_bytes(buffer.getvalue())
    return digest.hexdigest()


def main() -> None:
    query = json.load(sys.stdin)
    service = query["service"]
    directory = assemble(service, query["packages"].split(","), query["dependency_group"])
    target = BUILD_DIR / f"{service}.tar.gz"
    digest = archive(directory, target)
    json.dump({"directory": str(directory), "archive": str(target), "sha256": digest}, sys.stdout)


if __name__ == "__main__":
    main()
