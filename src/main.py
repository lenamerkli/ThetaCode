from src.docker import Docker
from requests import request
from src.venv_finder import scan_for_venvs, VenvInfo
from time import sleep
from pathlib import Path
from src.llm import T_CONVERSATION, load_prompt, LLM


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
        self.execute_in_docker(f"rm -rf {container_path}")
        self.execute_in_docker(f"python3 -m venv {container_path}")
        for pkg in venv.packages:
            pkg_name = pkg.get('name')
            pkg_version = pkg.get('version')
            if pkg_name and pkg_version:
                self.execute_in_docker(f"pip install {pkg_name}=={pkg_version}", venv=container_path, cwd=f"/home/agent/{self._project.name}")
            elif pkg_name:
                self.execute_in_docker(f"pip install {pkg_name}", venv=container_path, cwd=f"/home/agent/{self._project.name}")

    def _start_docker(self, recreate_venvs: bool = True):
        if not self._project:
            raise ValueError("Project not specified")
        self._docker.start(port=self._port, additional_volumes=[(self._project.path, f"/home/agent/{self._project.name}")])
        sleep(1)
        if recreate_venvs:
            venvs = self._get_venvs()
            for venv in venvs:
                self._recreate_venv(venv)

    def execute_in_docker(self, command: str, cwd: str = '/home/agent/', venv: str = '', timeout: int = 60):
        payload = {
            'command': command,
            'cwd': cwd,
            'venv': venv,
            'timeout': timeout
        }
        resp = request('post', f"http://localhost:{self._port}/execute", json=payload)
        return resp.json()

    def read_file(self, path: str) -> dict:
        resp = request('post', f"http://localhost:{self._port}/read_file", json={'path': path})
        return resp.json()

    def write_to_file(self, path: str, content: str) -> dict:
        resp = request('post', f"http://localhost:{self._port}/write_to_file", json={'path': path, 'content': content})
        return resp.json()

    def replace_in_file(self, path: str, search: str, replace: str) -> dict:
        resp = request('post', f"http://localhost:{self._port}/replace_in_file", json={'path': path, 'search': search, 'replace': replace})
        return resp.json()

    def health_check(self):
        resp = request('get', f"http://localhost:{self._port}/")
        return resp.status_code == 200

    def _get_venvs(self):
        return scan_for_venvs(self._project.path)

    def _stop_docker(self):
        self._docker.stop()


class Chat:
    def __init__(self, project: Project, theta_code: ThetaCode):
        self._project = project
        self._theta_code = theta_code
        self._conversation = []
        self._cost = 0.0

    def get_cost(self) -> float:
        return self._cost

    def _set_system_message(self, llm: LLM):
        # llm will later be used to disable features that are not supported by the selected LLM; to be implemented
        self._conversation[0] = {'role': 'system', 'content': load_prompt('system_default').replace('%%project_name%%', self._project.name)}

    def send_message(self, message: str, llm: LLM):
        self._set_system_message(llm)
        self._conversation.append({'role': 'user', 'content': f"<user_message>\n{message}\n</user_message>"})

    @staticmethod
    def _parse_tool_param(options: str, param_name: str, default_value: str = '') -> str:
        """Extract a parameter value from the tool call XML."""
        open_tag = f'<{param_name}>'
        close_tag = f'</{param_name}>'
        if open_tag in options and close_tag in options:
            return options.split(open_tag, 1)[-1].split(close_tag, 1)[0].strip()
        return default_value

    @staticmethod
    def _truncate(content: str, max_chars: int) -> str:
        """Truncate content to max_chars client-side. Returns the original if max_chars <= 0."""
        if max_chars <= 0:
            return content
        if len(content) > max_chars:
            return content[:max_chars]
        return content

    @staticmethod
    def _get_char_pos_for_line(content: str, line: int) -> int:
        """Return the 0-based character index for the start of the given 1-based line number.
        If line > number of lines, returns len(content)."""
        if line <= 1:
            return 0
        pos = 0
        current = 1
        while current < line and pos < len(content):
            idx = content.find('\n', pos)
            if idx == -1:
                return len(content)
            pos = idx + 1
            current += 1
        return pos

    @staticmethod
    def _get_char_pos_for_end_line(content: str, line: int) -> int:
        """Return the 0-based character index for the end of the given 1-based line number (exclusive).
        If line > number of lines, returns len(content)."""
        pos = Chat._get_char_pos_for_line(content, line)
        if pos >= len(content):
            return len(content)
        idx = content.find('\n', pos)
        if idx == -1:
            return len(content)
        return idx + 1 if idx != len(content) - 1 else len(content)

    def _step(self, llm: LLM):
        if self._conversation[-1]['role'] == 'user':
            response = llm.generate(self._conversation)
            self._cost += response['cost']
            self._conversation.append({'role': 'assistant', 'content': response['text'], 'thinking': response['thinking'], 'cost': response['cost'], 'llm': llm.model})
            self._step(llm)
        else:
            content = self._conversation[-1]['content']
            if ('<tool_call>' in content) and ('</tool_call>' in content):
                options = content.split('<tool_call>', 1)[-1].split('</tool_call>', 1)[0].strip()
                if ('<tool_name>' in content) and ('</tool_name>' in options):
                    tool_name = options.split('<tool_name>', 1)[-1].split('</tool_name>', 1)[0].strip()
                    tool_response = ''
                    match tool_name:
                        case 'read_file':
                            path = self._parse_tool_param(options, 'path')
                            start_line_str = self._parse_tool_param(options, 'start_line', '1')
                            end_line_str = self._parse_tool_param(options, 'end_line', '1000')
                            start_char_str = self._parse_tool_param(options, 'start_char', '0')
                            end_char_str = self._parse_tool_param(options, 'end_char', '100000')
                            max_chars_str = self._parse_tool_param(options, 'max_chars', '1000000')
                            start_line = int(start_line_str) if start_line_str else 1
                            end_line = int(end_line_str) if end_line_str else 1000
                            start_char = int(start_char_str) if start_char_str else 0
                            end_char = int(end_char_str) if end_char_str else 100000
                            max_chars = int(max_chars_str) if max_chars_str else 1000000
                            tool_response = self._tool_read_file(path, start_line, end_line, start_char, end_char, max_chars)
                        case 'write_to_file':
                            path = self._parse_tool_param(options, 'path')
                            content = self._parse_tool_param(options, 'content')
                            tool_response = self._tool_write_to_file(path, content)
                        case 'replace_in_file':
                            path = self._parse_tool_param(options, 'path')
                            search = self._parse_tool_param(options, 'search')
                            replace = self._parse_tool_param(options, 'replace')
                            tool_response = self._tool_replace_in_file(path, search, replace)
                        case 'bash':
                            command = self._parse_tool_param(options, 'command')
                            timeout_str = self._parse_tool_param(options, 'timeout', '60')
                            directory = self._parse_tool_param(options, 'directory', '/home/agent/')
                            venv = self._parse_tool_param(options, 'venv')
                            max_chars_str = self._parse_tool_param(options, 'max_chars', '100000')
                            timeout = int(timeout_str) if timeout_str else 60
                            max_chars = int(max_chars_str) if max_chars_str else 100000
                            tool_response = self._tool_bash(command, timeout, directory, venv, max_chars)
                        case 'ask_user':
                            pass
                        case _:
                            tool_response = f'Unknown tool: {tool_name}'
                    if tool_name != 'ask_user':
                        self._conversation.append({'role': 'user', 'content': f'<tool_response>\n{tool_response}\n</tool_response>'})
                        self._step(llm)
                else:
                    self._conversation.append({'role': 'user', 'content': load_prompt('tool_call_parsing_error')})
                    self._step(llm)
            else:
                self._conversation.append({'role': 'user', 'content': load_prompt('no_tool_call')})
                self._step(llm)

    def _tool_read_file(self, path: str, start_line: int = 1, end_line: int = 1000, start_char: int = 0, end_char: int = 100000, max_chars: int = 1000000) -> str:
        if not path:
            return 'Error: Missing path parameter'
        result = self._theta_code.read_file(path)
        if 'error' in result:
            return f'Error: {result["error"]}'
        content = result.get('content', '')
        start_line_pos = self._get_char_pos_for_line(content, start_line)
        end_line_pos = self._get_char_pos_for_end_line(content, end_line)
        start = max(start_line_pos, start_char)
        end = min(end_line_pos, end_char)
        if start < 0:
            start = 0
        if end > len(content):
            end = len(content)
        if start >= end:
            sliced = ''
        else:
            sliced = content[start:end]
        sliced = self._truncate(sliced, max_chars)
        return f"<path>{path}</path>\n<file_contents>{sliced}\n</file_contents>"

    def _tool_write_to_file(self, path: str, content: str) -> str:
        if not path:
            return 'Error: Missing path parameter'
        result = self._theta_code.write_to_file(path, content)
        if 'error' in result:
            return f'Error: {result["error"]}'
        return 'File written successfully'

    def _tool_replace_in_file(self, path: str, search: str, replace: str) -> str:
        if not path:
            return 'Error: Missing path parameter'
        if not search:
            return 'Error: Missing search parameter'
        result = self._theta_code.replace_in_file(path, search, replace)
        if 'error' in result:
            return f'Error: {result["error"]}'
        return 'File replaced successfully'

    def _tool_bash(self, command: str, timeout: int = 60, directory: str = '/home/agent/', venv: str = '', max_chars: int = 100000) -> str:
        if not command:
            return 'Error: Missing command parameter'
        result = self._theta_code.execute_in_docker(command, cwd=directory, venv=venv, timeout=timeout)
        if 'error' in result:
            return f'Error: {result["error"]}'
        stdout = result.get('stdout', '')
        stderr = result.get('stderr', '')
        returncode = result.get('returncode', 0)
        output_parts = []
        if stdout:
            output_parts.append(f'<stdout>\n{stdout}\n</stdout>')
        if stderr:
            output_parts.append(f'<stderr>\n{stderr}\n</stderr>')
        output_parts.append(f'<returncode>{returncode}</returncode>')
        full_output = '\n'.join(output_parts)
        full_output = self._truncate(full_output, max_chars)
        return full_output
