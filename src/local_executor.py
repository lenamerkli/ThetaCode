import os
import subprocess
import secrets
import typing as t
from pathlib import Path

RESOURCES_DIR = Path.home() / '.local' / 'share' / 'ThetaCode' / 'resources'
RESOURCES_VENV = Path.home() / '.local' / 'share' / 'ThetaCode' / 'resources_venv'
SOFTWARE_DIR = RESOURCES_DIR / 'software'
OPT_DIR = RESOURCES_DIR / 'opt'
EXAMPLES_DIR = RESOURCES_DIR / 'examples'

# Docker-host path mapping
DOCKER_PROJECT_BASE = '/home/agent'
DOCKER_SOFTWARE = '/home/agent/software'
DOCKER_OPT = '/opt/thetacode'
DOCKER_EXAMPLES = '/home/agent/examples'
DOCKER_TOOLS = '/home/agent/tools'
DOCKER_TMP = '/home/agent/tmp'

# Resources venv Python paths
RESOURCES_VENV_PYTHON = RESOURCES_VENV / 'bin' / 'python3'
RESOURCES_VENV_ACTIVATE = RESOURCES_VENV / 'bin' / 'activate'

REQUIRED_PACKAGES = [
    'flask',
    'requests',
    'beautifulsoup4',
    'curl_cffi',
    'crawl4ai',
    'playwright',
]


def _ensure_resources() -> None:
    """Set up the resources directory and venv on first local-mode launch."""
    RESOURCES_DIR.mkdir(parents=True, exist_ok=True)
    SOFTWARE_DIR.mkdir(parents=True, exist_ok=True)
    OPT_DIR.mkdir(parents=True, exist_ok=True)
    EXAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    (RESOURCES_DIR / 'tmp').mkdir(parents=True, exist_ok=True)
    (RESOURCES_DIR / 'tools').mkdir(parents=True, exist_ok=True)

    # Create venv if missing
    if not RESOURCES_VENV.is_dir():
        subprocess.run(
            ['python3', '-m', 'venv', str(RESOURCES_VENV)],
            check=True,
        )
        _install_resources_packages()

    # Symlink/write software scripts
    _write_software_wrappers()

    # Symlink opt scripts
    _symlink_opt_scripts()

    # Symlink examples
    _symlink_examples()


def _ensure_vm_resources() -> None:
    """Set up resources directly on the filesystem mirroring the Docker layout.

    In VM mode there is no path remapping — the LLM works with real host paths.
    Resources are placed under $HOME (software/, examples/, tools/, tmp/) and
    /opt/thetacode/ so they match the Docker container layout.
    """
    home = Path.home()

    vm_dirs = [
        home / 'software',
        home / 'examples',
        home / 'tools',
        home / 'tmp',
        Path('/opt/thetacode'),
    ]
    for d in vm_dirs:
        d.mkdir(parents=True, exist_ok=True)

    # Write software wrapper scripts into ~/software/
    src_software_dir = Path(__file__).parent / 'docker' / 'software'
    for script_name in ['search_the_web', 'webpage_to_markdown']:
        dest = home / 'software' / script_name
        wrapper = f'''#!/bin/bash
# ThetaCode VM software wrapper
source "{RESOURCES_VENV_ACTIVATE}"
exec python3 "/opt/thetacode/{script_name}.py" "$@"
'''
        dest.write_text(wrapper)
        dest.chmod(0o755)

    # Copy opt scripts into /opt/thetacode/
    src_opt_dir = Path(__file__).parent / 'docker' / 'opt'
    opt_dir = Path('/opt/thetacode')
    for py_file in src_opt_dir.glob('*.py'):
        dest = opt_dir / py_file.name
        if not dest.exists():
            dest.write_text(py_file.read_text())

    # Copy examples into ~/examples/
    src_examples_dir = Path(__file__).parent / 'docker' / 'examples'
    if src_examples_dir.exists():
        def copy_tree(src: Path, dst: Path) -> None:
            dst.mkdir(parents=True, exist_ok=True)
            for item in src.iterdir():
                target = dst / item.name
                if item.is_dir():
                    copy_tree(item, target)
                elif not target.exists():
                    target.write_text(item.read_text())
        copy_tree(src_examples_dir, home / 'examples')


def _install_resources_packages() -> None:
    """Install required packages into the resources venv."""
    for pkg in REQUIRED_PACKAGES:
        subprocess.run(
            [str(RESOURCES_VENV_PYTHON), '-m', 'pip', 'install', '--no-cache-dir', pkg],
            check=False,
        )
    # Install playwright browsers
    try:
        subprocess.run(
            [str(RESOURCES_VENV_PYTHON), '-m', 'playwright', 'install', 'chromium'],
            check=False,
        )
        subprocess.run(
            [str(RESOURCES_VENV_PYTHON), '-m', 'playwright', 'install-deps', 'chromium'],
            check=False,
        )
    except Exception:
        pass


def _write_software_wrappers() -> None:
    """Write bash wrapper scripts for custom software that source the resources venv."""
    src_dir = Path(__file__).parent / 'docker' / 'software'

    for script_name in ['search_the_web', 'webpage_to_markdown']:
        container_path = SOFTWARE_DIR / script_name
        wrapper = f'''#!/bin/bash
# ThetaCode local software wrapper
# Always uses the resources venv.
source "{RESOURCES_VENV_ACTIVATE}"
exec python3 "{OPT_DIR}/{script_name}.py" "$@"
'''
        container_path.write_text(wrapper)
        container_path.chmod(0o755)


def _symlink_opt_scripts() -> None:
    """Ensure the Python scripts from docker/opt are available in resources/opt."""
    src_opt_dir = Path(__file__).parent / 'docker' / 'opt'
    for py_file in src_opt_dir.glob('*.py'):
        dest = OPT_DIR / py_file.name
        if not dest.exists():
            dest.write_text(py_file.read_text())


def _symlink_examples() -> None:
    """Copy examples from docker/examples into resources/examples."""
    src_examples_dir = Path(__file__).parent / 'docker' / 'examples'
    if not src_examples_dir.exists():
        return

    def copy_tree(src: Path, dst: Path) -> None:
        dst.mkdir(parents=True, exist_ok=True)
        for item in src.iterdir():
            target = dst / item.name
            if item.is_dir():
                copy_tree(item, target)
            elif not target.exists():
                target.write_text(item.read_text())

    copy_tree(src_examples_dir, EXAMPLES_DIR)


def remap_path(path: str, project_name: str, project_path: str) -> str:
    """Remap a Docker-container path to a local host path.

    Paths starting with /home/agent/{project_name} are remapped to the
    project's original path.  Generic /home/agent paths are remapped to
    the resources directory.
    """
    path = path.strip()
    project_prefix = f"{DOCKER_PROJECT_BASE}/{project_name}"

    if path == DOCKER_PROJECT_BASE or path == f"{DOCKER_PROJECT_BASE}/":
        return project_path
    if path.startswith(f"{project_prefix}/"):
        relative = path[len(project_prefix):]
        return str(Path(project_path) / relative.lstrip('/'))
    if path.startswith(project_prefix):
        return project_path

    if path.startswith(DOCKER_SOFTWARE):
        relative = path[len(DOCKER_SOFTWARE):]
        return str(SOFTWARE_DIR / relative.lstrip('/'))
    if path.startswith(DOCKER_OPT):
        relative = path[len(DOCKER_OPT):]
        return str(OPT_DIR / relative.lstrip('/'))
    if path.startswith(DOCKER_EXAMPLES):
        relative = path[len(DOCKER_EXAMPLES):]
        return str(EXAMPLES_DIR / relative.lstrip('/'))
    if path.startswith(DOCKER_TOOLS):
        relative = path[len(DOCKER_TOOLS):]
        return str(RESOURCES_DIR / 'tools' / relative.lstrip('/'))
    if path.startswith(DOCKER_TMP):
        relative = path[len(DOCKER_TMP):]
        return str(RESOURCES_DIR / 'tmp' / relative.lstrip('/'))

    # Paths not mapped: return as-is (they might be absolute paths on the host already)
    return path


class LocalExecutor:
    """Provides the same interface as the Docker HTTP backend but executes
    commands and file operations directly on the local host."""

    def __init__(self, project_name: str, project_path: str, no_remap: bool = False):
        self.project_name = project_name
        self.project_path = project_path
        self._no_remap = no_remap
        if no_remap:
            _ensure_vm_resources()
        else:
            _ensure_resources()
        self.access_token = "local"  # not used, but keeps interface compatible

    def remap_path(self, path: str) -> str:
        if self._no_remap:
            return path
        return remap_path(path, self.project_name, self.project_path)

    def execute(
        self,
        command: str,
        cwd: str = '/home/agent/',
        venv: str = '',
        timeout: int = 60,
    ) -> dict:
        """Execute a bash command locally."""
        local_cwd = self.remap_path(cwd)

        # If a project venv is specified, source it first
        venv_prefix = ''
        if venv:
            local_venv = self.remap_path(venv)
            venv_activate = Path(local_venv) / 'bin' / 'activate'
            if venv_activate.exists():
                venv_prefix = f'source "{venv_activate}" && '

        # For commands that reference the custom software or opt scripts,
        # ensure the resources venv is activated *in addition* to any
        # project venv. The resources venv takes priority for the script
        # execution.
        full_command = command
        if venv_prefix:
            full_command = venv_prefix + command

        try:
            result = subprocess.run(
                full_command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=local_cwd,
            )
            return {
                'stdout': result.stdout,
                'stderr': result.stderr,
                'returncode': result.returncode,
            }
        except subprocess.TimeoutExpired:
            return {
                'stdout': '',
                'stderr': f'Command timed out after {timeout}s',
                'returncode': -1,
            }
        except Exception as e:
            return {
                'stdout': '',
                'stderr': str(e),
                'returncode': -1,
                'error': str(e),
            }

    def read_file(self, path: str) -> dict:
        """Read a file from the local filesystem."""
        local_path = self.remap_path(path)
        try:
            p = Path(local_path)
            if not p.exists():
                return {'error': f'File not found: {path}'}
            if not p.is_file():
                return {'error': f'Not a file: {path}'}
            content = p.read_text(encoding='utf-8', errors='replace')
            return {'content': content}
        except Exception as e:
            return {'error': str(e)}

    def write_to_file(self, path: str, content: str) -> dict:
        """Write a file to the local filesystem."""
        local_path = self.remap_path(path)
        try:
            p = Path(local_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding='utf-8')
            return {'characters': len(content)}
        except Exception as e:
            return {'error': str(e)}

    def replace_in_file(self, path: str, search: str, replace: str) -> dict:
        """Replace text in a local file."""
        local_path = self.remap_path(path)
        try:
            p = Path(local_path)
            if not p.exists():
                return {'error': f'File not found: {path}'}
            if not p.is_file():
                return {'error': f'Not a file: {path}'}
            current = p.read_text(encoding='utf-8')
            if search not in current:
                return {'error': 'Search text not found in file'}
            replacements = current.count(search)
            new_content = current.replace(search, replace)
            p.write_text(new_content, encoding='utf-8')
            return {'replacements': replacements}
        except Exception as e:
            return {'error': str(e)}

    def health_check(self) -> bool:
        """Always healthy in local mode."""
        return True

    def start(self) -> None:
        """No-op for local mode (ensures resources are set up)."""
        _ensure_resources()

    def stop(self) -> None:
        """No-op for local mode."""
        pass