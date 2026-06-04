import asyncio
import concurrent.futures
from pathlib import Path
import shutil

from docker import Docker
from requests import request
from venv_finder import scan_for_venvs, VenvInfo
from time import sleep
from llm import T_CONVERSATION, load_prompt, LLM, get_llm
from persistence import Database


class Project:
    def __init__(self, name: str, path: str):
        self.name = name
        self.path = path

    @classmethod
    def create(cls, name: str, original_path: str):
        dest = Path.home() / '.local/share/ThetaCode/projects' / name / 'files'
        dest.parent.mkdir(parents=True, exist_ok=True)
        if Path(original_path).is_dir():
            shutil.copytree(original_path, dest, dirs_exist_ok=True)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original_path, dest)
        return cls(name, str(dest))


class ThetaCode:
    def __init__(self, port: int = 50000):
        self._docker = Docker()
        self.project = None
        self._port = port

    def _recreate_venv(self, venv: VenvInfo):
        project_path = Path(self.project.path)
        venv_path = venv.path
        try:
            relative_path = venv_path.relative_to(project_path)
        except ValueError:
            return
        container_path = f"/home/agent/{self.project.name}/{relative_path.as_posix()}"
        self.execute_in_docker(f"rm -rf {container_path}")
        self.execute_in_docker(f"python3 -m venv {container_path}")
        for pkg in venv.packages:
            pkg_name = pkg.get('name')
            pkg_version = pkg.get('version')
            if pkg_name and pkg_version:
                self.execute_in_docker(f"pip install {pkg_name}=={pkg_version}", venv=container_path, cwd=f"/home/agent/{self.project.name}")
            elif pkg_name:
                self.execute_in_docker(f"pip install {pkg_name}", venv=container_path, cwd=f"/home/agent/{self.project.name}")

    def start_docker(self, recreate_venvs: bool = True):
        if not self.project:
            raise ValueError("Project not specified")
        self._docker.start(port=self._port, additional_volumes=[(self.project.path, f"/home/agent/{self.project.name}")])
        sleep(1)
        if recreate_venvs:
            venvs = self._get_venvs()
            for venv in venvs:
                self._recreate_venv(venv)

    def _headers(self) -> dict:
        return {'Authorization': f'Bearer {self._docker.access_token}'}

    def execute_in_docker(self, command: str, cwd: str = '/home/agent/', venv: str = '', timeout: int = 60):
        payload = {
            'command': command,
            'cwd': cwd,
            'venv': venv,
            'timeout': timeout
        }
        resp = request('post', f"http://localhost:{self._port}/execute", json=payload, headers=self._headers())
        return resp.json()

    def read_file(self, path: str) -> dict:
        resp = request('post', f"http://localhost:{self._port}/read_file", json={'path': path}, headers=self._headers())
        return resp.json()

    def write_to_file(self, path: str, content: str) -> dict:
        resp = request('post', f"http://localhost:{self._port}/write_to_file", json={'path': path, 'content': content}, headers=self._headers())
        return resp.json()

    def replace_in_file(self, path: str, search: str, replace: str) -> dict:
        resp = request('post', f"http://localhost:{self._port}/replace_in_file", json={'path': path, 'search': search, 'replace': replace}, headers=self._headers())
        return resp.json()

    def health_check(self):
        resp = request('get', f"http://localhost:{self._port}/", headers=self._headers())
        return resp.status_code == 200

    def _get_venvs(self):
        return scan_for_venvs(self.project.path)

    def stop_docker(self):
        self._docker.stop()


class Chat:
    def __init__(self, project: Project, theta_code: ThetaCode, db: Database, chat_id: int | None = None):
        self._project = project
        self._theta_code = theta_code
        self._conversation: list[dict] = []
        self._cost = 0.0
        self._db = db
        self._chat_id = chat_id
        self._llm_model = ""

    def load_history(self):
        """Load messages from DB into the conversation."""
        if self._chat_id is None:
            return
        msgs = self._db.get_messages(self._chat_id)
        self._conversation = []
        for msg in msgs:
            entry = {
                'role': msg['role'],
                'content': msg['content'],
                'thinking': msg.get('thinking') or '',
                'cost': msg.get('cost') or 0.0,
                'llm': msg.get('llm_model') or '',
            }
            self._conversation.append(entry)
            self._cost += entry.get('cost', 0.0)
        if not self._conversation or self._conversation[0].get('role') != 'system':
            self._conversation.insert(0, {'role': 'system', 'content': ''})

    def get_cost(self) -> float:
        return self._cost

    def _set_system_message(self, llm: LLM):
        self._conversation[0] = {'role': 'system', 'content': load_prompt('system_default').replace('%%project_name%%', self._project.name)}

    def send_message(self, message: str, llm: LLM):
        """Start the conversation with a user message. Call run_async() to step through."""
        self._set_system_message(llm)
        self._llm_model = llm.model
        self._conversation.append({'role': 'user', 'content': f"<user_message>\n{message}\n</user_message>"})
        if self._chat_id is not None:
            self._db.save_message(self._chat_id, 'user', message, '', 0.0, llm.model)

    def step_generator(self, llm: LLM):
        """Synchronous generator that yields events. Runs in a thread."""
        while True:
            if self._conversation[-1]['role'] == 'user':
                response = llm.generate(self._conversation)
                self._cost += response['cost']
                entry = {
                    'role': 'assistant',
                    'content': response['text'],
                    'thinking': response.get('thinking', ''),
                    'cost': response['cost'],
                    'llm': llm.model,
                }
                self._conversation.append(entry)
                if self._chat_id is not None:
                    self._db.save_message(
                        self._chat_id, entry['role'], entry['content'],
                        entry['thinking'], entry['cost'], entry['llm']
                    )
                yield {"type": "assistant", "content": response['text'], "thinking": response.get('thinking', ''), "cost": response['cost']}
            else:
                content = self._conversation[-1]['content']
                if ('<tool_call>' in content) and ('</tool_call>' in content):
                    options = content.split('<tool_call>', 1)[-1].split('</tool_call>', 1)[0].strip()
                    if ('<tool_name>' in options) and ('</tool_name>' in options):
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
                            yield {"type": "tool_call", "tool": tool_name, "response": tool_response}
                        else:
                            yield {"type": "ask_user", "question": self._parse_tool_param(options, 'question')}
                            return
                    else:
                        self._conversation.append({'role': 'user', 'content': load_prompt('tool_call_parsing_error')})
                        yield {"type": "tool_error", "error": "parsing"}
                else:
                    yield {"type": "done"}
                    return

    async def run_async(self, llm: LLM):
        """Async wrapper that bridges the sync generator to asyncio."""
        loop = asyncio.get_event_loop()
        queue = asyncio.Queue()
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = None
        done = False

        def _run_in_thread():
            for event in self.step_generator(llm):
                if done:
                    break
                loop.call_soon_threadsafe(queue.put_nowait, event)

        try:
            future = loop.run_in_executor(executor, _run_in_thread)
            while True:
                event = await queue.get()
                yield event
                if event["type"] in ("done", "ask_user"):
                    break
        finally:
            done = True
            if future:
                future.cancel()
            executor.shutdown(wait=False)

    @staticmethod
    def _parse_tool_param(options: str, param_name: str, default_value: str = '') -> str:
        open_tag = f'<{param_name}>'
        close_tag = f'</{param_name}>'
        if open_tag in options and close_tag in options:
            return options.split(open_tag, 1)[-1].split(close_tag, 1)[0].strip()
        return default_value

    @staticmethod
    def _truncate(content: str, max_chars: int) -> str:
        if max_chars <= 0:
            return content
        if len(content) > max_chars:
            return content[:max_chars]
        return content

    @staticmethod
    def _get_char_pos_for_line(content: str, line: int) -> int:
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
        pos = Chat._get_char_pos_for_line(content, line)
        if pos >= len(content):
            return len(content)
        idx = content.find('\n', pos)
        if idx == -1:
            return len(content)
        return idx + 1 if idx != len(content) - 1 else len(content)

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
