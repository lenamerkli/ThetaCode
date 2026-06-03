from src.docker import Docker
from requests import request
from src.venv_finder import scan_for_venvs, VenvInfo
from time import sleep
from pathlib import Path
from src.llm import T_CONVERSATION, load_prompt, get_llm


class Project:
    def __init__(self, name: str, path: str):
        self.name = name
        self.path = path

    @classmethod
    def create(cls, name: str, original_path: str):
        path = f"~/.local/share/ThetaCode/projects/{name}/files"
        Path(original_path).copy(path)
        return cls(name, path)


class ThetaCode:
    def __init__(self, port: int = 50000):
        self._docker = Docker()
        self._project = None
        self._port = port

    def _recreate_venv(self, venv: VenvInfo):
        project_path = Path(self._project.path)
        venv_path = venv.path
        try:
            relative_path = venv_path.relative_to(project_path)
        except ValueError:
            return
        container_path = f"/home/agent/{self._project.name}/{relative_path.as_posix()}"
        self._execute_in_docker(f"rm -rf {container_path}")
        self._execute_in_docker(f"python3 -m venv {container_path}")
        for pkg in venv.packages:
            pkg_name = pkg.get('name')
            pkg_version = pkg.get('version')
            if pkg_name and pkg_version:
                self._execute_in_docker(f"pip install {pkg_name}=={pkg_version}", venv=container_path, cwd=f"/home/agent/{self._project.name}")
            elif pkg_name:
                self._execute_in_docker(f"pip install {pkg_name}", venv=container_path, cwd=f"/home/agent/{self._project.name}")

    def _start_docker(self, recreate_venvs: bool = True):
        if not self._project:
            raise ValueError("Project not specified")
        self._docker.start(port=self._port, additional_volumes=[(self._project.path, f"/home/agent/{self._project.name}")])
        sleep(1)
        if recreate_venvs:
            venvs = self._get_venvs()
            for venv in venvs:
                self._recreate_venv(venv)

    def _execute_in_docker(self, command: str, cwd: str = '/home/agent/', venv: str = '', timeout: int = 60):
        payload = {
            'command': command,
            'cwd': cwd,
            'venv': venv,
            'timeout': timeout
        }
        resp = request('post', f"http://localhost:{self._port}/execute", json=payload)
        return resp.json()

    def health_check(self):
        resp = request('get', f"http://localhost:{self._port}/")
        return resp.status_code == 200

    def _get_venvs(self):
        return scan_for_venvs(self._project.path)

    def _stop_docker(self):
        self._docker.stop()


class Chat:
    def __init__(self, project: Project):
        self._project = project
        self._conversation = []

    def _set_system_message(self, llm: str):
        # llm will later be used to disable features that are not supported by the selected LLM; to be implemented
        self._conversation.append({'role': 'system', 'content': load_prompt('system_default').replace('%%project_name%%', self._project.name)})

    def send_message(self, message: str, llm: str):
        self._conversation.append({'role': 'user', 'content': message})
        llm = get_llm(llm)
        response = llm.generate(self._conversation)
