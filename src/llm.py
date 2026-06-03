from abc import ABC, abstractmethod
from requests import request
from os import environ
from pathlib import Path
import typing as t


T_CONVERSATION = t.List[t.Dict[str, str]]
T_COMPLETION = t.Dict[str, t.Union[str, int, float]]


class LLM(ABC):
    @abstractmethod
    def __init__(self, model: str):
        self.model = model

    @abstractmethod
    def generate(self, conversation: T_CONVERSATION) -> T_COMPLETION:
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
        message = response_json["choices"][0]["message"]
        return {
            "text": message.get("content") or "",
            "cost": response_json.get("usage", {}).get("cost", 0.0),
            "thinking": message.get("reasoning", ""),
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
