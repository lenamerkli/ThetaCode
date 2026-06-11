import shutil
import threading
import typing as t
from pathlib import Path
from time import sleep

from docker import Docker
from requests import request
from venv_finder import scan_for_venvs, VenvInfo
from llm import T_CONVERSATION, T_STREAM_CALLBACK, load_prompt, LLM


class Project:
    def __init__(self, name: str, path: str):
        self.name = name
        self.path = path

    @classmethod
    def create(cls, name: str, original_path: str) -> 'Project':
        dest = Path.home() / '.local' / 'share' / 'ThetaCode' / 'projects' / name / 'files'
        dest.mkdir(parents=True, exist_ok=True)
        src = Path(original_path)
        if src.is_dir():
            shutil.copytree(str(src), str(dest), dirs_exist_ok=True)
        return cls(name, str(dest))

    @classmethod
    def from_path(cls, name: str, path: str) -> 'Project':
        """Load an existing project directly from a path without copying."""
        return cls(name, path)


# Type alias for the "on_new_message" callback used by the GUI.
# Receives a message dict (same structure as _conversation entries + extra fields).
T_MSG_CALLBACK = t.Callable[[dict], None]


class ThetaCode:
    def __init__(self, port: int = 50000):
        self._docker = Docker()
        self._project = None
        self._port = port
        self._running = False

    def set_project(self, project: Project):
        self._project = project

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
                self.execute_in_docker(
                    f"pip install {pkg_name}=={pkg_version}",
                    venv=container_path,
                    cwd=f"/home/agent/{self._project.name}",
                )
            elif pkg_name:
                self.execute_in_docker(
                    f"pip install {pkg_name}",
                    venv=container_path,
                    cwd=f"/home/agent/{self._project.name}",
                )

    def start_docker(self, recreate_venvs: bool = True):
        if not self._project:
            raise ValueError("Project not specified")
        if self._running:
            return
        self._docker.start(
            port=self._port,
            additional_volumes=[(self._project.path, f"/home/agent/{self._project.name}")],
        )
        self._running = True
        sleep(1)
        if recreate_venvs:
            venvs = self._get_venvs()
            for venv in venvs:
                self._recreate_venv(venv)

    # Keep old name for backward compatibility
    def _start_docker(self, recreate_venvs: bool = True):
        self.start_docker(recreate_venvs)

    def _headers(self) -> dict:
        return {'Authorization': f'Bearer {self._docker.access_token}'}

    def execute_in_docker(self, command: str, cwd: str = '/home/agent/', venv: str = '', timeout: int = 60):
        payload = {
            'command': command,
            'cwd': cwd,
            'venv': venv,
            'timeout': timeout,
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

    def health_check(self) -> bool:
        try:
            resp = request('get', f"http://localhost:{self._port}/", headers=self._headers(), timeout=5)
            return resp.status_code == 200
        except Exception:
            return False

    def _get_venvs(self):
        return scan_for_venvs(self._project.path)

    def stop_docker(self):
        if self._running:
            self._docker.stop()
            self._running = False

    def _stop_docker(self):
        self.stop_docker()


class Chat:
    """Manages a single conversation with the LLM inside a ThetaCode session."""

    # Sentinel returned by send_message when the AI used ask_user and is
    # waiting for the next human input.
    WAITING_FOR_USER = "WAITING_FOR_USER"

    def __init__(self, project: Project, theta_code: ThetaCode):
        self._project = project
        self._theta_code = theta_code
        # First slot reserved for the system message (filled lazily).
        self._conversation: T_CONVERSATION = [{'role': 'system', 'content': ''}]
        self._cost = 0.0

    def get_cost(self) -> float:
        return self._cost

    def restore_messages(self, stored_messages: list[dict]):
        """Rebuild _conversation from rows fetched out of the DB.

        Stored messages use the same role/content/thinking keys but may not
        include all internal fields.  We reconstruct a clean conversation list
        that the LLM can continue from.
        """
        # Always keep a system placeholder at index 0; it will be overwritten
        # on the next LLM call.
        self._conversation = [{'role': 'system', 'content': ''}]
        self._cost = 0.0
        for msg in stored_messages:
            role = msg.get('role', '')
            content = msg.get('content', '')
            thinking = msg.get('thinking', '')
            cost = msg.get('cost', 0.0)
            llm_model = msg.get('llm_model', '')
            if role == 'system':
                self._conversation[0] = {'role': 'system', 'content': content}
            else:
                entry: dict = {'role': role, 'content': content}
                if thinking:
                    entry['thinking'] = thinking
                if cost:
                    entry['cost'] = cost
                    self._cost += cost
                if llm_model:
                    entry['llm'] = llm_model
                self._conversation.append(entry)

    def _set_system_message(self, llm: LLM):
        self._conversation[0] = {
            'role': 'system',
            'content': load_prompt('system_default').replace('%%project_name%%', self._project.name),
        }

    def send_message(
        self,
        message: str,
        llm: LLM,
        on_new_message: t.Optional[T_MSG_CALLBACK] = None,
    ) -> str:
        """Send a user message and drive the agentic loop.

        ``on_new_message`` is called for every new conversation entry so callers
        (e.g. the GUI or a persistence layer) can react immediately.

        Returns either the final assistant text response or ``Chat.WAITING_FOR_USER``
        if the AI called the ask_user tool.
        """
        self._set_system_message(llm)
        user_entry = {'role': 'user', 'content': f"<user_message>\n{message}\n</user_message>"}
        self._conversation.append(user_entry)
        if on_new_message:
            on_new_message(user_entry)

        return self._run_loop(llm, on_new_message)

    def send_message_stream(
        self,
        message: str,
        llm: LLM,
        on_token: T_STREAM_CALLBACK,
        on_new_message: t.Optional[T_MSG_CALLBACK] = None,
        cancel_event: t.Optional[threading.Event] = None,
    ) -> str:
        """Send a user message and drive the agentic loop with token streaming.

        ``on_token(content)`` is called for each content token as it arrives
        from the LLM. ``on_new_message`` is called for every new conversation
        entry so callers can react immediately.

        ``cancel_event``, when set, causes the streaming request and agentic
        loop to abort early.

        Returns either the final assistant text response or ``Chat.WAITING_FOR_USER``
        if the AI called the ask_user tool.
        """
        self._set_system_message(llm)
        user_entry = {'role': 'user', 'content': f"<user_message>\n{message}\n</user_message>"}
        self._conversation.append(user_entry)
        if on_new_message:
            on_new_message(user_entry)

        return self._run_loop(llm, on_new_message, on_token=on_token, cancel_event=cancel_event)

    # ------------------------------------------------------------------
    # Iterative agentic loop (replaces the old recursive _step)
    # ------------------------------------------------------------------

    def _run_loop(
        self,
        llm: LLM,
        on_new_message: t.Optional[T_MSG_CALLBACK] = None,
        on_token: t.Optional[T_STREAM_CALLBACK] = None,
        cancel_event: t.Optional[threading.Event] = None,
    ) -> str:
        """Drive the conversation forward until a final assistant response or
        an ask_user pause.  Returns the final assistant text or WAITING_FOR_USER.

        If ``on_token`` is provided, the LLM's response is streamed in real-time
        via ``generate_stream()``.  Otherwise a single ``generate()`` call is used.

        ``cancel_event``, when set, aborts streaming requests and exits the loop.
        """

        MAX_ITERATIONS = 50  # safety guard against infinite loops
        iters = 0

        while iters < MAX_ITERATIONS:
            # Check cancellation between iterations (tool calls)
            if cancel_event and cancel_event.is_set():
                for msg in reversed(self._conversation):
                    if msg['role'] == 'assistant':
                        return msg['content']
                return ''

            iters += 1
            last = self._conversation[-1]

            if last['role'] == 'user':
                # Ask the LLM for a response — stream if a token callback is given.
                if on_token is not None:
                    response = llm.generate_stream(self._conversation, on_token, cancel_event=cancel_event)
                else:
                    response = llm.generate(self._conversation)
                self._cost += response['cost']
                if 'nemotron' in llm.model.lower():
                    response['text'] = response['text'].replace('</invoke>', '</tool_call>')
                assistant_entry = {
                    'role': 'assistant',
                    'content': response['text'],
                    'thinking': response['thinking'],
                    'cost': response['cost'],
                    'llm': llm.model,
                }
                self._conversation.append(assistant_entry)
                if on_new_message:
                    on_new_message(assistant_entry)
                # Loop again to process the assistant message.
                continue

            # Last message is from the assistant – check for a tool call.
            content = last['content']
            if '<tool_call>' not in content or '</tool_call>' not in content:
                # No tool call → finished; also send "no tool call" prompt for
                # the next human turn.  But first check if the LLM simply gave
                # a clean final answer (no tool tags at all).
                # We treat a message without any tool tags as the final answer.
                # (If you want to enforce tool use, uncomment the block below.)
                # ----
                # no_tool_entry = {'role': 'user', 'content': load_prompt('no_tool_call')}
                # self._conversation.append(no_tool_entry)
                # if on_new_message:
                #     on_new_message(no_tool_entry)
                # continue
                # ----
                return content  # final natural-language answer

            options = content.split('<tool_call>', 1)[-1].split('</tool_call>', 1)[0].strip()

            if '<tool_name>' not in content or '</tool_name>' not in options:
                # Malformed tool call
                err_entry = {'role': 'user', 'content': load_prompt('tool_call_parsing_error')}
                self._conversation.append(err_entry)
                if on_new_message:
                    on_new_message(err_entry)
                continue

            tool_name = options.split('<tool_name>', 1)[-1].split('</tool_name>', 1)[0].strip()

            if tool_name == 'ask_user':
                # Extract the question and return; the GUI will feed back the
                # user's answer as the next send_message call.
                question = self._parse_tool_param(options, 'question', content)
                # Add a synthetic assistant message so the UI shows the question.
                ask_entry = {
                    'role': 'assistant',
                    'content': question,
                    'thinking': '',
                    'cost': 0.0,
                    'llm': llm.model,
                    '_ask_user': True,
                }
                # Replace the raw tool_call message with the question text so
                # the conversation looks cleaner when restored.
                self._conversation[-1] = ask_entry
                if on_new_message:
                    on_new_message(ask_entry)
                return Chat.WAITING_FOR_USER

            # Execute the tool.
            tool_response = self._dispatch_tool(tool_name, options)
            tool_entry = {
                'role': 'user',
                'content': f'<tool_response>\n{tool_response}\n</tool_response>',
                '_tool_name': tool_name,
            }
            self._conversation.append(tool_entry)
            if on_new_message:
                on_new_message(tool_entry)

        # Safety: if we hit MAX_ITERATIONS return last assistant content.
        for msg in reversed(self._conversation):
            if msg['role'] == 'assistant':
                return msg['content']
        return ''

    # ------------------------------------------------------------------
    # Tool dispatch
    # ------------------------------------------------------------------

    def _dispatch_tool(self, tool_name: str, options: str) -> str:
        match tool_name:
            case 'read_file':
                path = self._parse_tool_param(options, 'path')
                start_line = int(self._parse_tool_param(options, 'start_line', '1') or '1')
                end_line = int(self._parse_tool_param(options, 'end_line', '1000') or '1000')
                start_char = int(self._parse_tool_param(options, 'start_char', '0') or '0')
                end_char = int(self._parse_tool_param(options, 'end_char', '100000') or '100000')
                max_chars = int(self._parse_tool_param(options, 'max_chars', '1000000') or '1000000')
                return self._tool_read_file(path, start_line, end_line, start_char, end_char, max_chars)
            case 'write_to_file':
                path = self._parse_tool_param(options, 'path')
                content = self._parse_tool_param(options, 'content')
                return self._tool_write_to_file(path, content)
            case 'replace_in_file':
                path = self._parse_tool_param(options, 'path')
                search = self._parse_tool_param(options, 'search')
                replace = self._parse_tool_param(options, 'replace')
                return self._tool_replace_in_file(path, search, replace)
            case 'bash':
                command = self._parse_tool_param(options, 'command')
                timeout = int(self._parse_tool_param(options, 'timeout', '60') or '60')
                directory = self._parse_tool_param(options, 'directory', '/home/agent/')
                venv = self._parse_tool_param(options, 'venv')
                max_chars = int(self._parse_tool_param(options, 'max_chars', '100000') or '100000')
                return self._tool_bash(command, timeout, directory, venv, max_chars)
            case _:
                return f'Unknown tool: {tool_name}'

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------

    def _tool_read_file(self, path: str, start_line: int = 1, end_line: int = 1000,
                        start_char: int = 0, end_char: int = 100000, max_chars: int = 1000000) -> str:
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
        start = max(0, start)
        end = min(len(content), end)
        sliced = content[start:end] if start < end else ''
        sliced = self._truncate(sliced, max_chars)
        return f"<path>{path}</path>\n<file_contents>\n{sliced}\n</file_contents>"

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

    def _tool_bash(self, command: str, timeout: int = 60, directory: str = '/home/agent/',
                   venv: str = '', max_chars: int = 100000) -> str:
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
