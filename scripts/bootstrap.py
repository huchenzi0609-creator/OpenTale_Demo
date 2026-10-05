"""Install reproducible development dependencies in repository-local caches."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def bootstrap() -> None:
    uv = shutil.which("uv")
    npm = shutil.which("npm")
    if not uv or not npm:
        raise SystemExit("请先安装 uv、Node.js（含 npm），然后重新运行此命令。")
    env = os.environ.copy()
    env["UV_CACHE_DIR"] = str(ROOT / ".cache" / "uv")
    env["UV_PROJECT_ENVIRONMENT"] = str(ROOT / ".venv")
    env["UV_PYTHON_INSTALL_DIR"] = str(ROOT / ".toolchain" / "python")
    env["npm_config_cache"] = str(ROOT / ".cache" / "npm")
    env.pop("OPENTALE_API_KEY", None)
    subprocess.run([uv, "sync", "--locked", "--extra", "dev"], cwd=ROOT, env=env, check=True)
    frontend = ROOT / "frontend"
    if not (frontend / "package-lock.json").exists():
        raise SystemExit("缺少 frontend/package-lock.json；请保留已提交的依赖锁文件。")
    subprocess.run([npm, "ci"], cwd=frontend, env=env, check=True)
    if (ROOT / ".git").exists() and shutil.which("git"):
        current = subprocess.run(
            ["git", "config", "--local", "--get", "core.hooksPath"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if current.returncode == 1:
            subprocess.run(
                ["git", "config", "--local", "core.hooksPath", "scripts/hooks"], cwd=ROOT, check=True
            )
    print("依赖已准备好；未读取或验证 API 密钥。")


if __name__ == "__main__":
    bootstrap()
