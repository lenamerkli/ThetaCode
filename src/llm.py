import threading
from abc import ABC, abstractmethod
from requests import request
from os import environ
from pathlib import Path
import json
import typing as t
from importlib.util import find_spec

# App attribution for OpenRouter — these headers identify ThetaCode in
# OpenRouter's public rankings and analytics.
_APP_HTTP_REFERER = "https://github.com/lenamerkli/ThetaCode"
_APP_TITLE = "ThetaCode"
_APP_CATEGORIES = "cli-agent,programming-app"


T_CONVERSATION = t.List[t.Dict[str, str]]
T_COMPLETION = t.Dict[str, t.Union[str, int, float]]
T_STREAM_CALLBACK = t.Callable[[str], None]


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

    @staticmethod
    def _fix_specific_responses(model_name: str, message: str) -> str:
        if model_name.startswith('deepseek'):
            message = message.replace('tool_call_name>', 'tool_name>')
        if (model_name == 'z-ai/glm-5.2') and ('<tool_name>bash</tool_name>' in message):
            message = message.replace('</arg_value>', '</command>').replace('<tool_call>bash<tool_name>', '<tool_call><tool_name>')
        return message

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
        if '<tool_call>' in message['content']:
            message['content'] += '</tool_call>'
        message['content'] = self._fix_specific_responses(self.model, message['content'])
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
            data['stop'].append('</invoke>')
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

                    except json.JSONDecodeError:
                        # Ignore malformed JSON chunks
                        pass

                if stream_finished:
                    break
        finally:
            response.close()

        print('\n' + '=' * 30 + ' End OpenRouter Streaming Response ' + '=' * 30)
        if '<tool_call>' in full_content:
            full_content += '</tool_call>'
        full_content = self._fix_specific_responses(model_name, full_content)
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


def load_prompt(name: str) -> str:
    with open(Path(__file__).parent / 'prompts' / f"{name}.md", 'r', encoding='utf-8') as f:
        content = f.read()
    return content.strip()