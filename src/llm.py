import threading
from abc import ABC, abstractmethod
from requests import request
from os import environ
from pathlib import Path
import json
import typing as t
from importlib.util import find_spec

from toolcall_repair import repair

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


T_CONVERSATION = t.List[t.Dict[str, str]]
T_COMPLETION = t.Dict[str, t.Union[str, int, float]]
T_STREAM_CALLBACK = t.Callable[[str], None]


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
        for i in range(len(check_region) - window_size + 1):
            window = check_region[i:i + window_size]
            count = seen.get(window, 0) + 1
            seen[window] = count
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
    def generate(self, conversation: T_CONVERSATION) -> T_COMPLETION:
        pass

    @abstractmethod
    def generate_stream(
        self,
        conversation: T_CONVERSATION,
        on_token: T_STREAM_CALLBACK,
        cancel_event: t.Optional[threading.Event] = None,
    ) -> T_COMPLETION:
        """Stream tokens via on_token(content), return the final T_COMPLETION dict.
        
        If ``cancel_event`` is set, the stream is aborted early and a partial
        T_COMPLETION is returned with whatever was accumulated so far.
        """
        pass


class OpenRouterLLM(LLM):
    def __init__(self, model: str, headroom_enabled: bool = False):
        self.model = model
        self.headroom_enabled = headroom_enabled

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

    def generate(self, conversation: T_CONVERSATION) -> T_COMPLETION:
        conversation = self._compress_conversation(conversation)
        model_name = self.model.replace('OpenRouter', '').replace('openrouter', '')
        if model_name[0] == '/':
            model_name = model_name[1:]
        data = {
            'model': model_name,
            'messages': conversation,
            'stop': ['</tool_call>'],
            'provider': {'sort': 'price'},
        }
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
        message['content'] = message.get('content', '')
        if ('<tool_call>' in message['content']) and ('</tool_call>' not in message['content']):
            message['content'] += '</tool_call>'
        return {
            'text': message['content'],
            'cost': response_json.get('usage', {}).get('cost', 0.0),
            'thinking': message.get('reasoning', ''),
        }

    def generate_stream(
        self,
        conversation: T_CONVERSATION,
        on_token: T_STREAM_CALLBACK,
        cancel_event: t.Optional[threading.Event] = None,
    ) -> T_COMPLETION:
        """Stream tokens via on_token(content) and return the final T_COMPLETION.
        
        If ``cancel_event`` is set, the stream is aborted early and a partial
        T_COMPLETION is returned with whatever was accumulated so far.
        """
        # Compress before streaming
        conversation = self._compress_conversation(conversation)

        # Check cancellation before even starting the request
        if cancel_event and cancel_event.is_set():
            return {"text": "", "cost": 0.0, "thinking": ""}

        model_name = self.model.replace('OpenRouter', '').replace('openrouter', '')
        if model_name[0] == '/':
            model_name = model_name[1:]
        data: dict[str, t.Any] = {
            'model': model_name,
            'messages': conversation,
            'stream': True,
            'stop': ['</tool_call>'],
            'provider': {'sort': 'price'},
        }
        if model_name == 'z-ai/glm-5.2':
            data['stop'].extend(['</invoke>', '</parameter>', '<parameter'])
        print('=' * 30 + ' Begin OpenRouter Streaming Request ' + '=' * 30)
        print({'model': model_name, 'messages': f'[{len(conversation)} messages]', 'stream': True})
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
        if ('<tool_call>' in full_content) and ('</tool_call>' not in full_content):
            full_content += '</tool_call>'
        full_content = repair(full_content)
        return {
            "text": full_content,
            "cost": total_cost,
            "thinking": full_thinking,
        }


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
