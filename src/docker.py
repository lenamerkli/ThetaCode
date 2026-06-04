import subprocess
import os
import secrets
import typing as t


IMAGE_NAME = 'thetacode_debian_13'
CONTAINER_NAME = 'thetacode'
INTERNAL_PORT = 50000


class Docker:
    def __init__(self, image_name: str = IMAGE_NAME, container_name: str = CONTAINER_NAME, internal_port: int = INTERNAL_PORT):
        self.image_name = image_name
        self.container_name = container_name
        self.internal_port = internal_port
        self.access_token = f"sk-{secrets.token_hex(32)}"
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

    def start(self, port: int, additional_volumes: t.Optional[t.List[t.Tuple[str, str]]] = None, env: t.Optional[t.Dict[str, str]] = None):
        self._stop_existing_container()
        cmd = [
            'docker', 'run', '-d', '--name', self.container_name,
            '-p', f'{port}:{self.internal_port}',
            '-e', f'ACCESS_TOKEN={self.access_token}',
            '-v', f"/opt/jetbrains_gateway:/opt/jetbrains_gateway",
        ]
        if env:
            for key, value in env.items():
                cmd.extend(['-e', f'{key}={value}'])
        if additional_volumes:
            for host_path, container_path in additional_volumes:
                host_path = os.path.abspath(host_path)
                cmd.extend(['-v', f'{host_path}:{container_path}'])
        cmd.append(self.image_name)
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
