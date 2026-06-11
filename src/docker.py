import subprocess
import os
import secrets
import typing as t
from pathlib import Path


IMAGE_NAME = 'thetacode_debian_13'
CONTAINER_NAME = 'thetacode'
INTERNAL_PORT = 50000

TOKEN_DIR = Path.home() / '.local' / 'share' / 'ThetaCode'
TOKEN_FILE = TOKEN_DIR / 'access_token'

SSH_PUBLIC_KEY_FILES = [
    Path.home() / '.ssh' / 'id_ed25519.pub',
    Path.home() / '.ssh' / 'id_rsa.pub',
    Path.home() / '.ssh' / 'id_ecdsa.pub',
]


def _get_or_create_access_token() -> str:
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text().strip()
    token = f"sk-{secrets.token_hex(32)}"
    TOKEN_FILE.write_text(token)
    TOKEN_FILE.chmod(0o600)
    return token


def _get_host_ssh_public_keys() -> str:
    keys = []
    for key_file in SSH_PUBLIC_KEY_FILES:
        if key_file.exists():
            content = key_file.read_text().strip()
            if content:
                keys.append(content)
    return '\n'.join(keys)


class Docker:
    def __init__(self, image_name: str = IMAGE_NAME, container_name: str = CONTAINER_NAME, internal_port: int = INTERNAL_PORT):
        self.image_name = image_name
        self.container_name = container_name
        self.internal_port = internal_port
        self.access_token = _get_or_create_access_token()
        if not self._image_exists():
            self._build_image()

    def _image_exists(self) -> bool:
        try:
            result = subprocess.run(
                ['docker', 'images', '-q', self.image_name],
                capture_output=True,
                text=True,
                check=True
            )
            return bool(result.stdout.strip())
        except subprocess.CalledProcessError:
            return False

    def _build_image(self):
        docker_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'docker')
        dockerfile = os.path.join(docker_dir, 'DOCKERFILE')
        subprocess.run(
            ['docker', 'build', '-t', self.image_name, '-f', dockerfile, docker_dir],
            check=True
        )

    def start(self, port: int, ssh_port: int = 8022, additional_volumes: t.Optional[t.List[t.Tuple[str, str]]] = None, env: t.Optional[t.Dict[str, str]] = None):
        self._stop_existing_container()
        ssh_public_keys = _get_host_ssh_public_keys()
        cmd = [
            'docker', 'run', '-d', '--name', self.container_name,
            '-p', f'{port}:{self.internal_port}',
            '-p', f'{ssh_port}:22',
            '-e', f'ACCESS_TOKEN={self.access_token}',
            '-v', f"/opt/jetbrains_gateway:/opt/jetbrains_gateway",
        ]
        if ssh_public_keys:
            cmd.extend(['-e', f'SSH_PUBLIC_KEY={ssh_public_keys}'])
        if env:
            for key, value in env.items():
                cmd.extend(['-e', f'{key}={value}'])
        if additional_volumes:
            for host_path, container_path in additional_volumes:
                host_path = os.path.abspath(host_path)
                cmd.extend(['-v', f'{host_path}:{container_path}'])
        cmd.append(self.image_name)
        print(f"==== Docker Command Begin ====")
        print(cmd)
        print(f"==== Docker Command End ====")
        subprocess.run(cmd, check=True)

    def _stop_existing_container(self):
        result = subprocess.run(
            ['docker', 'ps', '-aq', '-f', f'name={self.container_name}'],
            capture_output=True,
            text=True
        )
        if result.stdout.strip():
            subprocess.run(['docker', 'stop', self.container_name], capture_output=True)
            subprocess.run(['docker', 'rm', self.container_name], capture_output=True)

    def stop(self):
        self._stop_existing_container()
