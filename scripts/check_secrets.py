"""Check public source and Git index without reading ignored private files.

This is a leak guard, not a guarantee that every possible credential is detected.
No matching value or source line is ever printed.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
MAX_SOURCE_BYTES = 8 * 1024 * 1024
TOKEN_PATTERNS = {
    "provider token": re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "GitHub token": re.compile(rb"\b(?:ghp_|github_pat_)[A-Za-z0-9_]{20,}\b"),
    "AWS access key": re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "private key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}
ASSIGNMENT = re.compile(
    rb"(?im)^[ \t]*(?:OPENTALE_API_KEY|OPENAI_API_KEY|DEEPSEEK_API_KEY)[ \t]*=[ \t]*[\"']?([^\s\"'#]+)"
)
PLACEHOLDERS = {b"", b"your-key", b"your-api-key", b"replace-me", b"changeme", b"example", b"dummy"}
PRIVATE_PARTS = {"runtime", ".venv", ".cache", ".toolchain", "node_modules", "__pycache__"}


def private_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    name = parts[-1] if parts else ""
    return (
        any(part in PRIVATE_PARTS for part in parts)
        or (name.startswith(".env") and name != ".env.example")
        or name.lower().endswith((".pem", ".p12", ".pfx", ".key"))
        or name == "server-process.json"
        or name.lower().endswith(".log")
    )


def content_findings(content: bytes) -> list[str]:
    findings = [label for label, pattern in TOKEN_PATTERNS.items() if pattern.search(content)]
    for match in ASSIGNMENT.finditer(content):
        value = match.group(1).lower()
        if value not in PLACEHOLDERS and not value.startswith((b"${", b"<")):
            findings.append("non-placeholder API key assignment")
            break
    return findings


def git(root: Path, *args: str) -> bytes:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError("无法检查本仓库的 Git 状态。")
    return result.stdout


def scan(root: Path) -> list[tuple[str, str]]:
    indexed = {name.decode("utf-8", "surrogateescape") for name in git(root, "ls-files", "-z", "--cached").split(b"\0") if name}
    public = {name.decode("utf-8", "surrogateescape") for name in git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split(b"\0") if name}
    findings: list[tuple[str, str]] = []
    for name in sorted(indexed | public):
        if private_path(name):
            # Do not read a secret file even if it was mistakenly added to Git.
            findings.append((name, "private file present in Git/public source selection"))
            continue
        path = root / name
        if path.is_symlink():
            findings.append((name, "public symlink cannot be inspected safely"))
            continue
        if name in public and path.is_file():
            if path.stat().st_size > MAX_SOURCE_BYTES:
                findings.append((name, "public source exceeds scanner size limit"))
            else:
                findings.extend((name, f"working tree: {label}") for label in content_findings(path.read_bytes()))
        if name in indexed:
            staged = git(root, "show", f":{name}")
            if len(staged) > MAX_SOURCE_BYTES:
                findings.append((name, "indexed source exceeds scanner size limit"))
            else:
                findings.extend((name, f"Git index: {label}") for label in content_findings(staged))
    return findings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        findings = scan(args.root.resolve())
    except (RuntimeError, OSError):
        raise SystemExit("密钥检查无法完成；请本地检查 Git/文件权限。未输出文件内容。") from None
    if findings:
        for name, reason in findings:
            print(f"FAIL {name!r}: {reason}")
        raise SystemExit(1)
    print("PASS：公开源码与 Git 暂存区未发现受检密钥模式或私有路径；未读取忽略文件。")


if __name__ == "__main__":
    main()
