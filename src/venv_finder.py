import os
import subprocess
import json
import sys
from pathlib import Path
from dataclasses import dataclass, field


@dataclass
class VenvInfo:
    path: Path
    python_version: str
    packages: list[dict[str, str]] = field(default_factory=list)


def find_python_executable(venv_path: Path) -> Path | None:
    candidates = [
        venv_path / "bin" / "python",
        venv_path / "bin" / "python3",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def is_venv(path: Path) -> bool:
    if (path / "pyvenv.cfg").is_file():
        return True
    if find_python_executable(path) is not None:
        if sys.platform == "win32":
            return (path / "Lib" / "site-packages").is_dir()
        else:
            lib = path / "lib"
            if lib.is_dir():
                return any(
                    (d / "site-packages").is_dir()
                    for d in lib.iterdir()
                    if d.is_dir()
                )
    return False


def get_venv_info(venv_path: Path) -> VenvInfo | None:
    python = find_python_executable(venv_path)
    if python is None:
        return None
    try:
        result = subprocess.run(
            [str(python), "--version"],
            capture_output=True, text=True, timeout=15,
        )
        version = result.stdout.strip() or result.stderr.strip()
    except Exception as exc:
        version = f"<error: {exc}>"
    packages: list[dict[str, str]] = []
    try:
        result = subprocess.run(
            [str(python), "-m", "pip", "list", "--format=json"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            packages = json.loads(result.stdout)
    except Exception:
        try:
            result = subprocess.run(
                [
                    str(python), "-c",
                    "import pkg_resources; "
                    "print([(d.project_name, d.version) for d in pkg_resources.working_set])"
                ],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0:
                for name, ver in eval(result.stdout.strip()):
                    packages.append({"name": name, "version": ver})
        except Exception:
            pass
    return VenvInfo(path=venv_path, python_version=version, packages=packages)


def scan_for_venvs(root_folder: str | Path, max_depth: int = 3, ) -> list[VenvInfo]:
    root = Path(root_folder).resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"{root} is not a directory")
    results: list[VenvInfo] = []
    def _walk(current: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            entries = sorted(current.iterdir())
        except PermissionError:
            return
        for entry in entries:
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            if is_venv(entry):
                info = get_venv_info(entry)
                if info:
                    results.append(info)
            else:
                _walk(entry, depth + 1)
    _walk(root, 0)
    return results
