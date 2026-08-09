import json
import os
import shutil
import threading
import typing as t
from pathlib import Path
from time import sleep

from docker import Docker, CONTAINER_IP
from local_executor import LocalExecutor, RESOURCES_DIR
from requests import request
from llm import (
    T_CONVERSATION, T_STREAM_CALLBACK, load_prompt, LLM,
    convert_conversation_to_legacy, convert_conversation_to_official,
    tool_calls_to_xml, xml_to_tool_calls,
)
from toolcall_repair import repair
from tool_definitions import supports_official_tool_calling, get_tools


class Project:
    def __init__(self, name: str, path: str, original_path: str | None = None,
                 mode: str = 'docker'):
        self.name = name
        self.path = path
        # original_path defaults to path for backward compatibility
        self.original_path = original_path or path
        self.mode = mode

    @classmethod
    def create(cls, name: str, original_path: str, mode: str = 'docker') -> 'Project':
        if mode == 'docker':
            dest = Path.home() / '.local' / 'share' / 'ThetaCode' / 'projects' / name / 'files'
            dest.mkdir(parents=True, exist_ok=True)
            src = Path(original_path)
            if src.is_dir():
                shutil.copytree(str(src), str(dest), dirs_exist_ok=True)
            return cls(name, str(dest), original_path=original_path, mode=mode)
        else:
            # Local / VM mode: work directly on the original path, no copying
            return cls(name, original_path, original_path=original_path, mode=mode)

    @classmethod
    def from_path(cls, name: str, path: str, original_path: str | None = None,
                  mode: str = 'docker') -> 'Project':
        """Load an existing project directly from a path without copying."""
        return cls(name, path, original_path=original_path, mode=mode)



# Type alias for the "on_new_message" callback used by the GUI.
# Receives a message dict (same structure as _conversation entries + extra fields).
T_MSG_CALLBACK = t.Callable[[dict], None]


class ThetaCode:
    def __init__(self, port: int = 50000, mode: str = 'docker'):
        self._mode = mode
        if mode == 'docker':
            self._backend = Docker()
        else:
            self._backend = None  # LocalExecutor created when project is set
        self._project = None
        self._port = port
        self._running = False

    def set_project(self, project: Project):
        self._project = project
        if self._mode == 'local' and self._backend is None:
            self._backend = LocalExecutor(project.name, project.original_path or project.path)
        elif self._mode == 'vm' and self._backend is None:
            self._backend = LocalExecutor(project.name, project.original_path or project.path, no_remap=True)

    def start(self, recreate_venvs: bool = True):
        if not self._project:
            raise ValueError("Project not specified")
        if self._running:
            return
        if self._mode == 'docker':
            env = {}
            if os.environ.get('BRAVE_API_KEY'):
                env['BRAVE_API_KEY'] = os.environ['BRAVE_API_KEY']
            self._backend.start(
                additional_volumes=[(self._project.path, f"/home/agent/{self._project.name}")],
                env=env if env else None,
            )
            self._running = True
            sleep(1)
            if recreate_venvs:
                from venv_finder import scan_for_venvs
                venvs = scan_for_venvs(self._project.path)
                for venv in venvs:
                    self._recreate_venv(venv)
        else:
            self._backend.start()
            self._running = True

    def _recreate_venv(self, venv):
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

    # Keep old name for backward compatibility
    def start_docker(self, recreate_venvs: bool = True):
        self.start(recreate_venvs)

    # Keep old name for backward compatibility
    def _start_docker(self, recreate_venvs: bool = True):
        self.start(recreate_venvs)

    def _headers(self) -> dict:
        return {'Authorization': f'Bearer {self._backend.access_token}'}

    def execute_in_docker(self, command: str, cwd: str = '/home/agent/', venv: str = '', timeout: int = 60):
        if self._mode == 'docker':
            payload = {
                'command': command,
                'cwd': cwd,
                'venv': venv,
                'timeout': timeout,
            }
            resp = request('post', f"http://{CONTAINER_IP}:{self._port}/execute", json=payload, headers=self._headers())
            return resp.json()
        else:
            return self._backend.execute(command=command, cwd=cwd, venv=venv, timeout=timeout)

    def read_file(self, path: str) -> dict:
        if self._mode == 'docker':
            resp = request('post', f"http://{CONTAINER_IP}:{self._port}/read_file", json={'path': path}, headers=self._headers())
            return resp.json()
        else:
            return self._backend.read_file(path)

    def write_to_file(self, path: str, content: str) -> dict:
        if self._mode == 'docker':
            resp = request('post', f"http://{CONTAINER_IP}:{self._port}/write_to_file", json={'path': path, 'content': content}, headers=self._headers())
            return resp.json()
        else:
            return self._backend.write_to_file(path, content)

    def replace_in_file(self, path: str, search: str, replace: str) -> dict:
        if self._mode == 'docker':
            resp = request('post', f"http://{CONTAINER_IP}:{self._port}/replace_in_file", json={'path': path, 'search': search, 'replace': replace}, headers=self._headers())
            return resp.json()
        else:
            return self._backend.replace_in_file(path, search, replace)

    def health_check(self) -> bool:
        if self._mode == 'docker':
            try:
                resp = request('get', f"http://{CONTAINER_IP}:{self._port}/", headers=self._headers(), timeout=5)
                return resp.status_code == 200
            except Exception:
                return False
        else:
            return self._backend.health_check()

    def stop(self):
        if self._running:
            self._backend.stop()
            self._running = False

    # Keep old name for backward compatibility
    def stop_docker(self):
        self.stop()

    def _stop_docker(self):
        self.stop()


class Chat:
    """Manages a single conversation with the LLM inside a ThetaCode session."""

    # Sentinel returned by send_message when the AI used ask_user and is
    # waiting for the next human input.
    WAITING_FOR_USER = "WAITING_FOR_USER"

    # Sentinel returned by _dispatch_tool when approval is needed (local mode only)
    NEEDS_APPROVAL = "NEEDS_APPROVAL"

    def __init__(self, project: Project, theta_code: ThetaCode,
                 on_approval_needed: t.Optional[t.Callable[[str, str], str]] = None):
        self._project = project
        self._theta_code = theta_code
        self._on_approval_needed = on_approval_needed
        # First slot reserved for the system message (filled lazily).
        self._conversation: T_CONVERSATION = [{'role': 'system', 'content': ''}]
        self._cost = 0.0
        # Rotate through tool_call_parsing_error variants to avoid repetition
        self._tool_call_parsing_error_count: int = 0
        self._tool_call_parsing_error_versions: int = self._detect_parsing_error_versions()

    def get_cost(self) -> float:
        return self._cost

    @staticmethod
    def _detect_parsing_error_versions() -> int:
        """Count how many tool_call_parsing_error.N.md files exist in prompts/."""
        prompts_dir = Path(__file__).parent / 'prompts'
        versions = 0
        v = 1
        while (prompts_dir / f'tool_call_parsing_error.{v}.md').exists():
            versions += 1
            v += 1
        return versions

    def _get_next_tool_call_parsing_error(self) -> str:
        """Rotate through available tool_call_parsing_error variants.

        First call returns version 1, second returns version 2, third returns
        version 1 again, etc.  Additional .3.md, .4.md files are picked up
        automatically.
        """
        self._tool_call_parsing_error_count += 1
        if self._tool_call_parsing_error_versions <= 1:
            return load_prompt('tool_call_parsing_error', version=1)
        version = ((self._tool_call_parsing_error_count - 1) % self._tool_call_parsing_error_versions) + 1
        return load_prompt('tool_call_parsing_error', version=version)

    def restore_messages(self, stored_messages: list[dict]):
        """Rebuild _conversation from rows fetched out of the DB.

        Stored messages are in official tool calling format:
        - Assistant messages may have 'tool_calls' (list of dicts)
        - Tool results have role='tool' with 'tool_call_id' and 'name'
        
        We reconstruct a clean conversation list that the LLM can continue from.
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
            tool_calls = msg.get('tool_calls')
            tool_call_id = msg.get('tool_call_id', '')
            name = msg.get('name', '')
            
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
                # Official tool calling fields
                if tool_calls:
                    entry['tool_calls'] = tool_calls
                if tool_call_id:
                    entry['tool_call_id'] = tool_call_id
                if name:
                    entry['name'] = name
                self._conversation.append(entry)

    def _set_system_message(self, llm: LLM, use_official_tools: bool = False):
        """Set the system message based on project mode and tool calling format.
        
        Args:
            llm: The LLM instance (used to determine model).
            use_official_tools: If True, use prompts without XML tool call instructions.
        """
        if self._project.mode == 'local':
            prompt_name = 'system_default_local_official' if use_official_tools else 'system_default_local'
        elif self._project.mode == 'vm':
            prompt_name = 'system_default_vm_official' if use_official_tools else 'system_default_vm'
        else:
            prompt_name = 'system_default_official' if use_official_tools else 'system_default'
        
        try:
            content = load_prompt(prompt_name).replace('%%project_name%%', self._project.name)
        except FileNotFoundError:
            # Fall back to legacy prompt if official prompt doesn't exist
            if self._project.mode == 'local':
                prompt_name = 'system_default_local'
            elif self._project.mode == 'vm':
                prompt_name = 'system_default_vm'
            else:
                prompt_name = 'system_default'
            content = load_prompt(prompt_name).replace('%%project_name%%', self._project.name)
        if self._project.mode == 'vm':
            content = content.replace('%%project_path%%', self._project.original_path or self._project.path)
        if not content.endswith('\n'):
            content += '\n'
        software_dir = Path(__file__).parent / 'docker' / 'software'
        software = []
        if software_dir.is_dir():
            for script_path in sorted(software_dir.iterdir()):
                if not script_path.is_file():
                    continue
                name = script_path.name
                if self._project.mode == 'vm':
                    src_path = f"~/{name}"
                else:
                    src_path = f'/home/agent/software/{name}'
                software.append(src_path)
        examples_dir = Path(__file__).parent / 'docker' / 'examples'
        examples = []
        if examples_dir.is_dir():
            for md_path in sorted(examples_dir.rglob('*.md')):
                relative = str(md_path.relative_to(examples_dir))
                if self._project.mode == 'vm':
                    src_path = f'~/{relative}'
                else:
                    src_path = f'/home/agent/examples/{relative}'
                examples.append(src_path)
        if software:
            content += '\n# Available Additional Software\n'
            content += 'The following command-line tools are available in addition to the standard Linux tools:\n - '
            content += '\n - '.join(software) + '\n'
        if examples:
            content += '\n# Available Example Documentation\n'
            content += 'The following example guides can be read with the `read_file` tool:\n - '
            content += '\n - '.join(examples) + '\n'

        self._conversation[0] = {
            'role': 'system',
            'content': content,
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
    # Resume / Iterative agentic loop
    # ------------------------------------------------------------------

    def resume(
        self,
        llm: LLM,
        on_new_message: t.Optional[T_MSG_CALLBACK] = None,
        on_token: t.Optional[T_STREAM_CALLBACK] = None,
        cancel_event: t.Optional[threading.Event] = None,
    ) -> str:
        """Resume the agentic loop from the current conversation state.

        Does NOT append a new user message — it picks up wherever the
        conversation left off: if the last message is from the assistant
        (with a tool call), the tool is parsed and executed; if the last
        message is from the user or a tool result, the LLM generates the
        next assistant response.
        """
        self._set_system_message(llm)
        return self._run_loop(llm, on_new_message, on_token=on_token, cancel_event=cancel_event)

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
        
        The conversation is stored internally in official tool calling format:
        - Assistant messages may have 'tool_calls' (list of dicts)
        - Tool results have role='tool' with 'tool_call_id'
        
        For models that don't support official tool calling, the conversation
        is converted to legacy XML format on-the-fly for the API request.
        """
        # Determine if this model supports official tool calling
        use_official_tools = supports_official_tool_calling(llm.model)
        
        # Set the appropriate system message
        self._set_system_message(llm, use_official_tools)

        MAX_ITERATIONS = 200  # safety guard against infinite loops
        iters = 0

        while iters < MAX_ITERATIONS:
            # Check cancellation between iterations (tool calls)
            if cancel_event and cancel_event.is_set():
                for msg in reversed(self._conversation):
                    if msg['role'] == 'assistant':
                        return msg.get('content', '') or ''
                return ''

            iters += 1
            last = self._conversation[-1]

            # Determine if we need to call the LLM
            needs_llm_call = last['role'] in ('user', 'system', 'tool')

            if needs_llm_call:
                # Prepare conversation for the API
                if use_official_tools:
                    # Use official format directly
                    api_conversation = self._conversation
                else:
                    # Convert to legacy XML format
                    api_conversation = convert_conversation_to_legacy(self._conversation)
                
                # Ask the LLM for a response — stream if a token callback is given.
                if on_token is not None:
                    response = llm.generate_stream(
                        api_conversation, on_token, cancel_event=cancel_event,
                        use_official_tools=use_official_tools
                    )
                else:
                    response = llm.generate(api_conversation, use_official_tools=use_official_tools)
                
                self._cost += response['cost']
                
                # Build assistant entry in official format
                assistant_entry: dict = {
                    'role': 'assistant',
                    'content': response['text'],
                    'thinking': response['thinking'],
                    'cost': response['cost'],
                    'llm': llm.model,
                }
                
                if use_official_tools:
                    # Official mode: tool_calls come from the API response
                    if response.get('tool_calls'):
                        assistant_entry['tool_calls'] = response['tool_calls']
                else:
                    # Legacy mode: parse XML tool calls from content and convert to official format
                    content = repair(response['text'])
                    text_content, tool_calls = xml_to_tool_calls(content)
                    if tool_calls:
                        assistant_entry['content'] = text_content
                        assistant_entry['tool_calls'] = tool_calls
                    else:
                        assistant_entry['content'] = content
                
                self._conversation.append(assistant_entry)
                if on_new_message:
                    on_new_message(assistant_entry)
                # Loop again to process the assistant message.
                continue

            # Last message is from the assistant – check for a tool call.
            tool_calls = last.get('tool_calls')
            
            if not tool_calls:
                # No tool call - use no_tool_call.md prompt
                no_tool_entry = {'role': 'user', 'content': load_prompt('no_tool_call')}
                self._conversation.append(no_tool_entry)
                if on_new_message:
                    on_new_message(no_tool_entry)
                continue

            # Process the first tool call (single tool call per turn)
            tool_call = tool_calls[0]
            tool_call_id = tool_call.get('id', '')
            func = tool_call.get('function', {})
            tool_name = func.get('name', '')
            
            try:
                tool_args = json.loads(func.get('arguments', '{}'))
            except json.JSONDecodeError:
                tool_args = {}

            if tool_name == 'ask_user':
                return Chat.WAITING_FOR_USER

            # Execute the tool using the args dict
            tool_response = self._dispatch_tool_from_args(tool_name, tool_args)
            if tool_response == Chat.NEEDS_APPROVAL:
                # Approval was denied by the user — return the last answer
                self._conversation[-1]['_approval_denied'] = True
                return f"[User denied the {tool_name} operation.]"
            
            # Store tool result in official format
            tool_entry = {
                'role': 'tool',
                'content': tool_response,
                'tool_call_id': tool_call_id,
                'name': tool_name,
            }
            self._conversation.append(tool_entry)
            if on_new_message:
                on_new_message(tool_entry)

        # Safety: if we hit MAX_ITERATIONS return last assistant content.
        for msg in reversed(self._conversation):
            if msg['role'] == 'assistant':
                return msg.get('content', '') or ''
        return ''

    # ------------------------------------------------------------------
    # Tool dispatch
    # ------------------------------------------------------------------

    def _dispatch_tool_from_args(self, tool_name: str, args: dict) -> str:
        """Dispatch a tool call using arguments from official tool calling format.
        
        Args:
            tool_name: Name of the tool to call.
            args: Dictionary of tool arguments.
            
        Returns:
            Tool response string, or NEEDS_APPROVAL if approval is required.
        """
        # In local mode, bash and write_to_file require user approval
        if self._project.mode == 'local' and self._on_approval_needed:
            if tool_name in ('bash', 'write_to_file'):
                command = args.get('command', '')
                path = args.get('path', '')
                content = args.get('content', '')

                if tool_name == 'bash' and command:
                    preview = f"Command: {command}"
                elif tool_name == 'write_to_file' and path:
                    preview = f"Write file: {path}\nContent: {content[:500]}..."
                else:
                    preview = f"{tool_name} operation"

                result = self._on_approval_needed(tool_name, preview)
                if result == 'denied':
                    return Chat.NEEDS_APPROVAL
                # approved — fall through to execute
            elif tool_name == 'replace_in_file':
                path = args.get('path', '')
                if path:
                    search = args.get('search', '')
                    preview = f"Edit file: {path}\nSearch: {search[:200]}..."
                    result = self._on_approval_needed(tool_name, preview)
                    if result == 'denied':
                        return Chat.NEEDS_APPROVAL

        match tool_name:
            case 'read_file':
                path = args.get('path', '')
                if not path:
                    return 'Error: Missing required parameter: path'
                start_line = int(args.get('start_line', 1) or 1)
                end_line = int(args.get('end_line', 1000) or 1000)
                start_char = int(args.get('start_char', 0) or 0)
                end_char = int(args.get('end_char', 100000) or 100000)
                max_chars = int(args.get('max_chars', 1000000) or 1000000)
                return self._tool_read_file(path, start_line, end_line, start_char, end_char, max_chars)
            case 'write_to_file':
                path = args.get('path', '')
                content = args.get('content', '')
                if not path:
                    return 'Error: Missing required parameter: path'
                if not content:
                    return 'Error: Missing required parameter: content'
                return self._tool_write_to_file(path, content)
            case 'replace_in_file':
                path = args.get('path', '')
                search = args.get('search', '')
                replace = args.get('replace', '')
                if not path:
                    return 'Error: Missing required parameter: path'
                if not search:
                    return 'Error: Missing required parameter: search'
                if not replace:
                    return 'Error: Missing required parameter: replace'
                return self._tool_replace_in_file(path, search, replace)
            case 'bash':
                command = args.get('command', '')
                if not command:
                    return 'Error: Missing required parameter: command'
                timeout = int(args.get('timeout', 60) or 60)
                directory = args.get('directory', '/home/agent/')
                venv = args.get('venv', '')
                max_chars = int(args.get('max_chars', 100000) or 100000)
                return self._tool_bash(command, timeout, directory, venv, max_chars)
            case _:
                return f'Unknown tool: {tool_name}'

    def _dispatch_tool(self, tool_name: str, options: str) -> str:
        # In local mode, bash and write_to_file require user approval
        if self._project.mode == 'local' and self._on_approval_needed:
            if tool_name in ('bash', 'write_to_file'):
                command = self._parse_tool_param(options, 'command')
                path = self._parse_tool_param(options, 'path')
                content = self._parse_tool_param(options, 'content')

                if tool_name == 'bash' and command:
                    preview = f"Command: {command}"
                elif tool_name == 'write_to_file' and path:
                    preview = f"Write file: {path}\nContent: {content[:500]}..."
                else:
                    preview = f"{tool_name} operation"

                result = self._on_approval_needed(tool_name, preview)
                if result == 'denied':
                    return Chat.NEEDS_APPROVAL
                # approved — fall through to execute
            elif tool_name == 'replace_in_file':
                path = self._parse_tool_param(options, 'path')
                if path:
                    search = self._parse_tool_param(options, 'search')
                    preview = f"Edit file: {path}\nSearch: {search[:200]}..."
                    result = self._on_approval_needed(tool_name, preview)
                    if result == 'denied':
                        return Chat.NEEDS_APPROVAL

        match tool_name:
            case 'read_file':
                if error_message := self._has_parameters(options, ['path']):
                    return error_message
                path = self._parse_tool_param(options, 'path')
                start_line = int(self._parse_tool_param(options, 'start_line', '1') or '1')
                end_line = int(self._parse_tool_param(options, 'end_line', '1000') or '1000')
                start_char = int(self._parse_tool_param(options, 'start_char', '0') or '0')
                end_char = int(self._parse_tool_param(options, 'end_char', '100000') or '100000')
                max_chars = int(self._parse_tool_param(options, 'max_chars', '1000000') or '1000000')
                return self._tool_read_file(path, start_line, end_line, start_char, end_char, max_chars)
            case 'write_to_file':
                if error_message := self._has_parameters(options, ['path', 'content']):
                    return error_message
                path = self._parse_tool_param(options, 'path')
                content = self._parse_tool_param(options, 'content')
                return self._tool_write_to_file(path, content)
            case 'replace_in_file':
                if error_message := self._has_parameters(options, ['path', 'search', 'replace']):
                    return error_message
                path = self._parse_tool_param(options, 'path')
                search = self._parse_tool_param(options, 'search')
                replace = self._parse_tool_param(options, 'replace')
                return self._tool_replace_in_file(path, search, replace)
            case 'bash':
                if error_message := self._has_parameters(options, ['command']):
                    return error_message
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
    def _has_parameters(options, parameters):
        for param in parameters:
            if (f"<{param}>" not in options) or (f"</{param}>" not in options):
                return f"Missing required parameter: <{param}> ... </{param}>"
        return ''

    @staticmethod
    def _parse_tool_param(options: str, param_name: str, default_value: str = '') -> str:
        """Extract a parameter value from the tool call XML."""
        open_tag = f'<{param_name}>'
        close_tag = f'</{param_name}>'
        if open_tag in options and close_tag in options:
            return options.split(open_tag, 1)[-1].rsplit(close_tag, 1)[0].strip()
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
        return f"<path>{path}</path>\n<note>Read {len(sliced)} of {len(content)} characters and {len(sliced.split('\n'))} of {len(content.split('\n'))} lines.</note>\n<file_contents>\n{sliced}\n</file_contents>"

    def _tool_write_to_file(self, path: str, content: str) -> str:
        if not path:
            return 'Error: Missing path parameter'
        result = self._theta_code.write_to_file(path, content)
        if 'error' in result:
            return f'Error: {result["error"]}'
        return f"{result['characters']} characters written to `{path}`."

    def _tool_replace_in_file(self, path: str, search: str, replace: str) -> str:
        if not path:
            return 'Error: Missing path parameter'
        if not search:
            return 'Error: Missing search parameter'
        result = self._theta_code.replace_in_file(path, search, replace)
        if 'error' in result:
            return f'Error: {result["error"]}'
        if result['replacements'] < 1:
            return 'Nothing was replaced in `{path}`.'
        return f"{result['replacements']} occurrences replaced in `{path}`."

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
