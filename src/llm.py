import threading
import uuid
from abc import ABC, abstractmethod
from requests import request
from os import environ
from pathlib import Path
import json
import typing as t
from importlib.util import find_spec

from toolcall_repair import repair
from tool_definitions import get_tools, supports_official_tool_calling

# App attribution for OpenRouter — these headers identify ThetaCode in
# OpenRouter's public rankings and analytics.
_APP_HTTP_REFERER = "https://github.com/lenamerkli/ThetaCode"
_APP_TITLE = "ThetaCode"
_APP_CATEGORIES = "cli-agent,programming-app"

REPETITION_CHECK_SPAN = 10000     # how far back to search (chars)
# (window_size, min_occurrences) — shorter windows need more hits
REPETITION_RULES = [
    (30, 30),
    (50, 25),
    (80, 15),
    (150, 12),
    (300, 6),
    (1000, 4),
]


T_CONVERSATION = t.List[t.Dict[str, t.Any]]
T_COMPLETION = t.Dict[str, t.Union[str, int, float, list, None]]
T_STREAM_CALLBACK = t.Callable[[str], None]
T_TOOL_CALL = t.Dict[str, t.Any]


def _find_repeating_window(text: str) -> tuple[int, str] | tuple[None, None]:
    """Find the earliest-position repeating window in the check region.

    Scans all window sizes defined in REPETITION_RULES across the tail
    portion of ``text``.  Returns the earliest character offset (relative
    to the start of the text) where any window begins, along with the
    matching window string, or ``(None, None)`` if nothing repeats.
    """
    if len(text) < 30:
        return None, None
    check_start = max(0, len(text) - REPETITION_CHECK_SPAN)
    check_region = text[check_start:]

    for window_size, min_occurrences in REPETITION_RULES:
        if len(check_region) < window_size * min_occurrences:
            continue
        seen: dict[str, int] = {}
        last_pos: dict[str, int] = {}
        for i in range(len(check_region) - window_size + 1):
            window = check_region[i:i + window_size]
            # Don't count overlapping windows of the same content
            prev = last_pos.get(window, -window_size)
            if i - prev < window_size:
                continue
            count = seen.get(window, 0) + 1
            seen[window] = count
            last_pos[window] = i
            if count >= min_occurrences:
                return check_start + i, window
    return None, None


def _detect_repetition(text: str) -> bool:
    """Return True if any window-size rule detects repetition."""
    found_pos, _ = _find_repeating_window(text)
    return found_pos is not None


def _force_close_response(response) -> None:
    """Force-close the underlying TCP connection of a streaming response.

    Python ``requests``' ``response.close()`` can block trying to drain
    the socket.  We bypass that by closing the raw urllib3 connection first,
    which sends a TCP RST so the server immediately sees the disconnection
    and cancels generation.
    """
    try:
        # Close the urllib3 HTTPResponse (the raw socket)
        response.raw.close()
    except Exception:
        pass
    try:
        response.close()
    except Exception:
        pass


class LLM(ABC):
    @abstractmethod
    def __init__(self, model: str):
        self.model = model

    @abstractmethod
    def generate(self, conversation: T_CONVERSATION, use_official_tools: bool = False) -> T_COMPLETION:
        pass

    @abstractmethod
    def generate_stream(
        self,
        conversation: T_CONVERSATION,
        on_token: T_STREAM_CALLBACK,
        cancel_event: t.Optional[threading.Event] = None,
        use_official_tools: bool = False,
    ) -> T_COMPLETION:
        """Stream tokens via on_token(content), return the final T_COMPLETION dict.
        
        If ``cancel_event`` is set, the stream is aborted early and a partial
        T_COMPLETION is returned with whatever was accumulated so far.
        
        If ``use_official_tools`` is True, the request uses OpenAI-style tool
        calling and the response may contain ``tool_calls`` instead of text.
        """
        pass


# ---------------------------------------------------------------------------
# Format conversion utilities (official <-> legacy XML)
# ---------------------------------------------------------------------------

def tool_calls_to_xml(tool_calls: list[T_TOOL_CALL]) -> str:
    """Convert official tool_calls to legacy XML format.
    
    Args:
        tool_calls: List of tool call dicts in OpenAI format:
            [{"id": "...", "type": "function", "function": {"name": "...", "arguments": "{...}"}}]
    
    Returns:
        XML string with <tool_call> blocks.
    """
    parts = []
    for tc in tool_calls:
        func = tc.get('function', {})
        name = func.get('name', '')
        try:
            args = json.loads(func.get('arguments', '{}'))
        except json.JSONDecodeError:
            args = {}
        
        lines = ['<tool_call>', f'<tool_name>{name}</tool_name>']
        for key, value in args.items():
            lines.append(f'<{key}>{value}</{key}>')
        lines.append('</tool_call>')
        parts.append('\n'.join(lines))
    return '\n'.join(parts)


def xml_to_tool_calls(content: str) -> tuple[str, list[T_TOOL_CALL]]:
    """Parse legacy XML tool calls from content and convert to official format.
    
    Args:
        content: Text that may contain <tool_call> XML blocks.
        
    Returns:
        Tuple of (text_content_without_tool_calls, list_of_tool_calls).
        The tool_calls are in OpenAI format with generated IDs.
    """
    import re
    
    tool_calls = []
    # Find all tool_call blocks
    pattern = r'<tool_call>(.*?)</tool_call>'
    matches = list(re.finditer(pattern, content, re.DOTALL))
    
    if not matches:
        return content, []
    
    # Extract text before first tool call
    text_before = content[:matches[0].start()].strip()
    
    for match in matches:
        block = match.group(1)
        
        # Extract tool name
        name_match = re.search(r'<tool_name>(.*?)</tool_name>', block, re.DOTALL)
        if not name_match:
            continue
        tool_name = name_match.group(1).strip()
        
        # Extract parameters (any tag that's not tool_name)
        args = {}
        param_pattern = r'<(\w+)>(.*?)</\1>'
        for param_match in re.finditer(param_pattern, block, re.DOTALL):
            param_name = param_match.group(1)
            if param_name != 'tool_name':
                args[param_name] = param_match.group(2).strip()
        
        tool_calls.append({
            'id': f'call_{uuid.uuid4().hex[:24]}',
            'type': 'function',
            'function': {
                'name': tool_name,
                'arguments': json.dumps(args)
            }
        })
    
    return text_before, tool_calls


def convert_message_to_official(msg: dict) -> dict:
    """Convert a single message from legacy format to official format.
    
    Handles:
    - Assistant messages with XML tool calls -> tool_calls field
    - User messages with <tool_response> -> role: "tool"
    
    Args:
        msg: Message dict with role/content.
        
    Returns:
        Message dict in official format.
    """
    role = msg.get('role', '')
    content = msg.get('content', '')
    result = dict(msg)
    
    if role == 'assistant' and '<tool_call>' in content:
        # Parse XML tool calls
        text_content, tool_calls = xml_to_tool_calls(content)
        if tool_calls:
            result['content'] = text_content if text_content else None
            result['tool_calls'] = tool_calls
    
    elif role == 'user' and content.lstrip().startswith('<tool_response>'):
        # Convert tool response to official format
        # Extract the content between tags
        import re
        match = re.search(r'<tool_response>\s*(.*?)\s*</tool_response>', content, re.DOTALL)
        if match:
            result['role'] = 'tool'
            result['content'] = match.group(1)
            # tool_call_id will be set by the caller based on context
            # We mark it for later processing
            result['_needs_tool_call_id'] = True
    
    return result


def convert_message_to_legacy(msg: dict) -> dict:
    """Convert a single message from official format to legacy XML format.
    
    Handles:
    - Assistant messages with tool_calls -> XML in content
    - Tool messages -> user messages with <tool_response>
    
    Args:
        msg: Message dict in official format.
        
    Returns:
        Message dict in legacy format.
    """
    role = msg.get('role', '')
    result = dict(msg)
    
    if role == 'assistant' and msg.get('tool_calls'):
        # Convert tool_calls to XML
        xml_content = tool_calls_to_xml(msg['tool_calls'])
        existing_content = msg.get('content') or ''
        result['content'] = (existing_content + '\n' + xml_content).strip() if existing_content else xml_content
        result.pop('tool_calls', None)
    
    elif role == 'tool':
        # Convert to user message with tool_response tags
        result['role'] = 'user'
        result['content'] = f'<tool_response>\n{msg.get("content", "")}\n</tool_response>'
        result.pop('tool_call_id', None)
        result.pop('name', None)
    
    return result


def convert_conversation_to_legacy(conversation: T_CONVERSATION) -> T_CONVERSATION:
    """Convert an entire conversation from official to legacy format.
    
    Args:
        conversation: List of messages in official format.
        
    Returns:
        List of messages in legacy format.
    """
    return [convert_message_to_legacy(msg) for msg in conversation]


def convert_conversation_to_official(conversation: T_CONVERSATION) -> T_CONVERSATION:
    """Convert an entire conversation from legacy to official format.
    
    This also links tool responses to their corresponding tool calls.
    
    Args:
        conversation: List of messages in legacy format.
        
    Returns:
        List of messages in official format.
    """
    result = []
    pending_tool_call_ids = []  # Queue of tool call IDs waiting for responses
    
    for msg in conversation:
        converted = convert_message_to_official(msg)
        
        # Track tool calls for linking responses
        if converted.get('role') == 'assistant' and converted.get('tool_calls'):
            for tc in converted['tool_calls']:
                pending_tool_call_ids.append(tc['id'])
        
        # Link tool responses to their calls
        if converted.get('_needs_tool_call_id'):
            converted.pop('_needs_tool_call_id', None)
            if pending_tool_call_ids:
                converted['tool_call_id'] = pending_tool_call_ids.pop(0)
            else:
                # Generate a placeholder ID if no matching call found
                converted['tool_call_id'] = f'call_{uuid.uuid4().hex[:24]}'
        
        result.append(converted)
    
    return result


class OpenRouterLLM(LLM):
    def __init__(self, model: str, headroom_enabled: bool = False):
        self.model = model
        self.headroom_enabled = headroom_enabled
        # Parse optional :provider suffix from model name (e.g.
        # 'openrouter/model-slug:deepinfra').  The provider controls
        # OpenRouter's provider.order / allow_fallbacks routing.
        temp = self.model
        for prefix in ('OpenRouter/', 'openrouter/'):
            if temp.startswith(prefix):
                temp = temp[len(prefix):]
                break
        if ':' in temp:
            self._provider = temp.split(':', 1)[1]
        else:
            self._provider = None

    @staticmethod
    def _attribution_headers() -> dict:
        """Build the optional OpenRouter app-attribution headers."""
        headers = {"HTTP-Referer": _APP_HTTP_REFERER}
        if _APP_TITLE:
            headers["X-OpenRouter-Title"] = _APP_TITLE
        if _APP_CATEGORIES:
            headers["X-OpenRouter-Categories"] = _APP_CATEGORIES
        return headers


    def _compress_conversation(self, conversation: T_CONVERSATION) -> T_CONVERSATION:
        """Compress the conversation using headroom if available and enabled."""
        if not self.headroom_enabled:
            return conversation
        if not find_spec('headroom'):
            return conversation
        try:
            from headroom import compress
            result = compress(
                conversation,
                model=self.model.split('/')[-1],
                compress_system_messages=False,
                protect_recent=2,
                compress_user_messages=True,
            )
            if result.tokens_saved > 0:
                print(f"[Headroom] Saved {result.tokens_saved} tokens "
                      f"({result.compression_ratio:.1%} ratio)")
            return result.messages
        except Exception as e:
            print(f"[Headroom] Compression skipped: {e}")
            return conversation

    def generate(self, conversation: T_CONVERSATION, use_official_tools: bool = False) -> T_COMPLETION:
        """Generate a completion from the LLM.
        
        Args:
            conversation: List of message dicts.
            use_official_tools: If True, use OpenAI-style tool calling.
            
        Returns:
            Dict with 'text', 'cost', 'thinking', and optionally 'tool_calls'.
        """
        conversation = self._compress_conversation(conversation)
        model_name = self.model.replace('OpenRouter', '').replace('openrouter', '')
        if model_name[0] == '/':
            model_name = model_name[1:]
        # Strip optional :provider suffix — it's handled via routing fields below
        if ':' in model_name:
            model_name = model_name.split(':', 1)[0]
        
        data: dict[str, t.Any] = {
            'model': model_name,
            'messages': conversation,
            'provider': {'sort': 'price'},
        }
        if self._provider:
            data['provider'] = {
                'order': [self._provider],
                'allow_fallbacks': False,
            }
        
        if use_official_tools:
            # Official tool calling: include tools, no stop sequence
            data['tools'] = get_tools()
        else:
            # Legacy XML mode: use stop sequence for tool_call tag
            data['stop'] = ['</tool_call>']
        
        print('=' * 30 + ' Begin OpenRouter Request ' + '=' * 30)
        print(data)
        print('=' * 30 + ' End OpenRouter Request ' + '=' * 30)
        request_headers = {
            'Content-Type': 'application/json',
            'Authorization': 'Bearer ' + environ['OPENROUTER_API_KEY'],
        }
        request_headers.update(self._attribution_headers())
        response = request(
            method='POST',
            url='https://openrouter.ai/api/v1/chat/completions',
            headers=request_headers,
            json=data,
            verify='/etc/pki/tls/certs/ca-bundle.crt',
        )
        if response.status_code != 200:
            raise Exception(response.text)
        response_json = response.json()
        print('=' * 30 + ' Begin OpenRouter Response ' + '=' * 30)
        print(response_json)
        print('=' * 30 + ' End OpenRouter Response ' + '=' * 30)
        message = response_json["choices"][0]["message"]
        
        result: T_COMPLETION = {
            'text': message.get('content', '') or '',
            'cost': response_json.get('usage', {}).get('cost', 0.0),
            'thinking': message.get('reasoning', ''),
        }
        
        if use_official_tools:
            # Handle official tool calls
            if message.get('tool_calls'):
                result['tool_calls'] = message['tool_calls']
        else:
            # Legacy XML mode: repair malformed tool calls
            content = result['text']
            if ('<tool_call>' in content) and ('</tool_call>' not in content):
                content += '</tool_call>'
            result['text'] = content
        
        return result

    def generate_stream(
        self,
        conversation: T_CONVERSATION,
        on_token: T_STREAM_CALLBACK,
        cancel_event: t.Optional[threading.Event] = None,
        use_official_tools: bool = False,
    ) -> T_COMPLETION:
        """Stream tokens via on_token(content) and return the final T_COMPLETION.
        
        If ``cancel_event`` is set, the stream is aborted early and a partial
        T_COMPLETION is returned with whatever was accumulated so far.
        
        If ``use_official_tools`` is True, the request uses OpenAI-style tool
        calling and the response may contain ``tool_calls`` instead of text.
        """
        # Compress before streaming
        conversation = self._compress_conversation(conversation)

        # Check cancellation before even starting the request
        if cancel_event and cancel_event.is_set():
            return {"text": "", "cost": 0.0, "thinking": ""}

        model_name = self.model.replace('OpenRouter', '').replace('openrouter', '')
        if model_name[0] == '/':
            model_name = model_name[1:]
        # Strip optional :provider suffix — it's handled via routing fields below
        if ':' in model_name:
            model_name = model_name.split(':', 1)[0]
        
        data: dict[str, t.Any] = {
            'model': model_name,
            'messages': conversation,
            'stream': True,
            'provider': {'sort': 'price'},
        }
        if self._provider:
            data['provider'] = {
                'order': [self._provider],
                'allow_fallbacks': False,
            }
        
        if use_official_tools:
            # Official tool calling: include tools, no stop sequence
            data['tools'] = get_tools()
        else:
            # Legacy XML mode: use stop sequence for tool_call tag
            data['stop'] = ['</tool_call>']
            if model_name == 'z-ai/glm-5.2':
                data['stop'].extend(['</invoke>', '</parameter>', '<parameter'])
        
        print('=' * 30 + ' Begin OpenRouter Streaming Request ' + '=' * 30)
        print({'model': model_name, 'messages': f'[{len(conversation)} messages]', 'stream': True, 'official_tools': use_official_tools})
        print('=' * 30 + ' End OpenRouter Streaming Request ' + '=' * 30)

        stream_request_headers = {
            'Content-Type': 'application/json',
            'Authorization': 'Bearer ' + environ['OPENROUTER_API_KEY'],
        }
        stream_request_headers.update(self._attribution_headers())
        response = request(
            method='POST',
            url='https://openrouter.ai/api/v1/chat/completions',
            headers=stream_request_headers,
            json=data,
            verify='/etc/pki/tls/certs/ca-bundle.crt',
            stream=True,
        )

        # Handle pre-stream HTTP errors
        if response.status_code != 200:
            raise Exception(f"OpenRouter error {response.status_code}: {response.text}")

        full_content = ""
        full_thinking = ""
        total_cost = 0.0
        
        # For official tool calling: accumulate tool call chunks
        # Tool calls arrive as deltas with index, id, function.name, function.arguments
        tool_calls_acc: dict[int, dict] = {}  # index -> accumulated tool call

        buffer = bytearray()
        stream_finished = False
        stream_cancelled = False
        try:
            for chunk_bytes in response.iter_content(chunk_size=1024):
                # Check for cancellation on each chunk
                if cancel_event and cancel_event.is_set():
                    print("\n[Stream cancelled by user]")
                    break

                if chunk_bytes:
                    buffer.extend(chunk_bytes)

                # Process all complete lines from the buffer
                while True:
                    nl_idx = buffer.find(b'\n')
                    if nl_idx == -1:
                        break

                    line_bytes = buffer[:nl_idx]
                    del buffer[:nl_idx + 1]

                    # Decode the complete line (safe — no split multi-byte chars)
                    line = line_bytes.decode('utf-8').rstrip('\r')

                    if not line:
                        continue

                    # Skip SSE comments (e.g. ": OPENROUTER PROCESSING")
                    if line.startswith(':'):
                        continue

                    if not line.startswith('data: '):
                        continue

                    data_str = line[6:]
                    if data_str == '[DONE]':
                        stream_finished = True
                        break

                    try:
                        chunk = json.loads(data_str)

                        # Check for mid-stream error
                        if 'error' in chunk:
                            print(f"Stream error: {chunk['error'].get('message', 'unknown error')}")
                            stream_finished = True
                            break

                        delta = chunk.get('choices', [{}])[0].get('delta', {})

                        # Accumulate content and thinking
                        content = delta.get('content', '') or ''
                        thinking = delta.get('reasoning', '') or ''

                        if content:
                            full_content += content
                            print(content, end="", flush=True)
                            on_token(content)

                        if thinking:
                            full_thinking += thinking
                            print(thinking, end="", flush=True)
                        
                        # Accumulate tool calls (official mode)
                        if use_official_tools and delta.get('tool_calls'):
                            for tc_delta in delta['tool_calls']:
                                idx = tc_delta.get('index', 0)
                                if idx not in tool_calls_acc:
                                    tool_calls_acc[idx] = {
                                        'id': tc_delta.get('id', ''),
                                        'type': 'function',
                                        'function': {'name': '', 'arguments': ''}
                                    }
                                tc = tool_calls_acc[idx]
                                if tc_delta.get('id'):
                                    tc['id'] = tc_delta['id']
                                func_delta = tc_delta.get('function', {})
                                if func_delta.get('name'):
                                    tc['function']['name'] += func_delta['name']
                                if func_delta.get('arguments'):
                                    tc['function']['arguments'] += func_delta['arguments']

                        # Grab usage/cost from any chunk that includes it
                        usage = chunk.get('usage', {}) or {}
                        if 'cost' in usage:
                            total_cost = usage['cost']

                        # ---- Repetition detection ---------------------------
                        if _detect_repetition(full_thinking + full_content):
                            print("\n==== STREAM CANCELLED DUE TO REPETITION ====\n")
                            stream_cancelled = True
                            _force_close_response(response)
                            break

                    except json.JSONDecodeError:
                        # Ignore malformed JSON chunks
                        pass

                if stream_finished or stream_cancelled:
                    break
        finally:
            if not stream_cancelled:
                response.close()

        print('\n' + '=' * 30 + ' End OpenRouter Streaming Response ' + '=' * 30)
        
        result: T_COMPLETION = {
            "text": full_content,
            "cost": total_cost,
            "thinking": full_thinking,
        }
        
        if use_official_tools:
            # Return accumulated tool calls
            if tool_calls_acc:
                result['tool_calls'] = [tool_calls_acc[i] for i in sorted(tool_calls_acc.keys())]
        else:
            # Legacy XML mode: repair malformed tool calls
            if ('<tool_call>' in full_content) and ('</tool_call>' not in full_content):
                full_content += '</tool_call>'
            full_content = repair(full_content)
            result['text'] = full_content
        
        return result


def get_llm(model: str, headroom_enabled: bool = False) -> LLM:
    if model.startswith('OpenRouter') or model.startswith('openrouter'):
        return OpenRouterLLM(model, headroom_enabled=headroom_enabled)
    else:
        raise ValueError(f"Unknown model: {model}")


def load_prompt(name: str, version: int | None = None) -> str:
    filename = f"{name}.{version}.md" if version is not None else f"{name}.md"
    with open(Path(__file__).parent / 'prompts' / filename, 'r', encoding='utf-8') as f:
        content = f.read()
    return content.strip()
