from abc import ABC, abstractmethod
from requests import request
from os import environ
from pathlib import Path
import json
import typing as t


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
    ) -> T_COMPLETION:
        """Stream tokens via on_token(content), return the final T_COMPLETION dict."""
        pass


class OpenRouterLLM(LLM):
    def __init__(self, model: str):
        self.model = model

    def generate(self, conversation: T_CONVERSATION) -> T_COMPLETION:
        model_name = self.model.replace('OpenRouter', '').replace('openrouter', '')
        if model_name[0] == '/':
            model_name = model_name[1:]
        data = {
            'model': model_name,
            'messages': conversation
        }
        print('=' * 30 + ' Begin OpenRouter Request ' + '=' * 30)
        print(data)
        print('=' * 30 + ' End OpenRouter Request ' + '=' * 30)
        response = request(
            method='POST',
            url='https://openrouter.ai/api/v1/chat/completions',
            headers={
                'Content-Type': 'application/json',
                'Authorization': 'Bearer ' + environ['OPENROUTER_API_KEY'],
            },
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
        return {
            "text": message.get("content") or "",
            "cost": response_json.get("usage", {}).get("cost", 0.0),
            "thinking": message.get("reasoning", ""),
        }

    def generate_stream(
        self,
        conversation: T_CONVERSATION,
        on_token: T_STREAM_CALLBACK,
    ) -> T_COMPLETION:
        """Stream tokens via on_token(content) and return the final T_COMPLETION."""
        model_name = self.model.replace('OpenRouter', '').replace('openrouter', '')
        if model_name[0] == '/':
            model_name = model_name[1:]
        data = {
            'model': model_name,
            'messages': conversation,
            'stream': True,
        }
        print('=' * 30 + ' Begin OpenRouter Streaming Request ' + '=' * 30)
        print({'model': model_name, 'messages': f'[{len(conversation)} messages]', 'stream': True})
        print('=' * 30 + ' End OpenRouter Streaming Request ' + '=' * 30)

        response = request(
            method='POST',
            url='https://openrouter.ai/api/v1/chat/completions',
            headers={
                'Content-Type': 'application/json',
                'Authorization': 'Bearer ' + environ['OPENROUTER_API_KEY'],
            },
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

        try:
            for line in response.iter_lines(decode_unicode=True):
                if not line:
                    continue

                # Skip SSE comments (e.g. ": OPENROUTER PROCESSING")
                if line.startswith(':'):
                    continue

                if not line.startswith('data: '):
                    continue

                data_str = line[6:]
                if data_str == '[DONE]':
                    break

                try:
                    chunk = json.loads(data_str)

                    # Check for mid-stream error
                    if 'error' in chunk:
                        print(f"Stream error: {chunk['error'].get('message', 'unknown error')}")
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
        finally:
            response.close()

        print('=' * 30 + ' End OpenRouter Streaming Response ' + '=' * 30)

        return {
            "text": full_content,
            "cost": total_cost,
            "thinking": full_thinking,
        }


def get_llm(model: str) -> LLM:
    if model.startswith('OpenRouter') or model.startswith('openrouter'):
        return OpenRouterLLM(model)
    else:
        raise ValueError(f"Unknown model: {model}")


def load_prompt(name: str) -> str:
    with open(Path(__file__).parent / 'prompts' / f"{name}.md", 'r', encoding='utf-8') as f:
        content = f.read()
    return content.strip()
