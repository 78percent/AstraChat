"""Check indexed source boundaries without printing credential values."""
from __future__ import annotations

from pathlib import PurePosixPath
from io import BytesIO
import re
import subprocess


PRIVATE_ROOTS = frozenset({"runtime", ".runtime", ".venv", "work", "reports", "output", "tmp", "openspec", ".agents"})
SECRET = re.compile(rb"(?:\b(?:sk|ghp|gho|ghu|ghs)-?[A-Za-z0-9_]{32,}\b|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)")


def inspect(path: str, data: bytes) -> list[str]:
    parts = PurePosixPath(path).parts
    reasons = []
    if (parts[0] in PRIVATE_ROOTS or "node_modules" in parts or "__pycache__" in parts
            or "dist" in parts or ".local." in parts[-1] or parts[-1] == ".env"
            or (parts[-1].startswith(".env.") and parts[-1] != ".env.example")
            or parts[-1].endswith((".sqlite", ".sqlite3", ".db", ".pyc", ".log"))
            or path.startswith("personas/review/")):
        reasons.append("private_or_generated_path")
    if len(data) > 10 * 1024 * 1024:
        reasons.append("large_file_review_required")
    if SECRET.search(data):
        reasons.append("credential_pattern")
    return reasons


def indexed_files():
    paths = subprocess.check_output(["git", "ls-files", "-z"]).decode("utf-8").split("\0")
    paths = [p for p in paths if p]
    if not paths:
        return
    # A single Git process avoids spawning one child per file on Windows.
    output = subprocess.check_output(["git", "cat-file", "--batch"], input="".join(":" + p + "\n" for p in paths).encode("utf-8"))
    stream = BytesIO(output)
    for path in paths:
        header = stream.readline().split()
        if len(header) != 3 or header[1] != b"blob":
            raise ValueError("indexed_blob_unavailable")
        yield path, stream.read(int(header[2]))
        if stream.read(1) != b"\n":
            raise ValueError("invalid_git_batch_response")


def main() -> int:
    files = list(indexed_files())
    if not files:
        print("No indexed files. Stage the intended source files first.")
        return 1
    failures = 0
    for path, data in files:
        for reason in inspect(path, data):
            print(f"{path}: {reason}")
            failures += 1
    print(f"Checked {len(files)} indexed files; {failures} findings. Values are never printed.")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
