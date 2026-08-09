"""Shared tool definitions for both official and legacy tool calling.

This module defines all available tools in a single source of truth.
The definitions can be exported as:
- OpenAI-compatible JSON Schema for official tool calling
- Prompt text for legacy XML-based tool calling
"""

import fnmatch
import json
from pathlib import Path
from typing import Any

# Tool definitions in OpenAI function calling format
TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Execute a bash shell command.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The command to execute"
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "The timeout for the command in seconds",
                        "default": 60
                    },
                    "directory": {
                        "type": "string",
                        "description": "The working directory to execute the command in",
                        "default": "/home/agent/"
                    },
                    "venv": {
                        "type": "string",
                        "description": "The python virtual environment to execute the command in"
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "The maximum number of characters of output. It will cut off the entire tool response, not just stdout.",
                        "default": 100000
                    }
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file. If both start_line and start_char are provided, the one further from the start will be used. If both end_line and end_char are provided, the one further from the end will be used.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "The path to the file to read"
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "The line to start reading from, 1-indexed",
                        "default": 1
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "The line to end reading at",
                        "default": 1000
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "The maximum number of characters to read",
                        "default": 1000000
                    },
                    "start_char": {
                        "type": "integer",
                        "description": "The character to start reading from, 0-indexed",
                        "default": 0
                    },
                    "end_char": {
                        "type": "integer",
                        "description": "The character to end reading at",
                        "default": 100000
                    }
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_to_file",
            "description": "Write contents to a file. The file will be newly created or completely overwritten.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "The path to the file to create or overwrite"
                    },
                    "content": {
                        "type": "string",
                        "description": "The content to write"
                    }
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "replace_in_file",
            "description": "This is the main method to edit files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "The path to the file to edit"
                    },
                    "search": {
                        "type": "string",
                        "description": "The content to replace (must match exactly, no regex search)"
                    },
                    "replace": {
                        "type": "string",
                        "description": "The content to write"
                    }
                },
                "required": ["path", "search", "replace"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": "Ask the user a question. Use for clarification or if you are stuck somewhere. Also use this tool call if you are finished, just ask if the user is satisfied with your work.",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The question to ask the user."
                    }
                },
                "required": ["question"]
            }
        }
    }
]


def get_tools() -> list[dict[str, Any]]:
    """Return the list of tools in OpenAI function calling format."""
    return TOOLS


def get_tool_names() -> list[str]:
    """Return the list of tool names."""
    return [tool["function"]["name"] for tool in TOOLS]


def get_tool_schema(tool_name: str) -> dict[str, Any] | None:
    """Return the JSON schema for a specific tool."""
    for tool in TOOLS:
        if tool["function"]["name"] == tool_name:
            return tool["function"]["parameters"]
    return None


# Model support for official tool calling
_MODELS_CACHE: dict | None = None


def _load_models_config() -> dict:
    """Load the tool calling models configuration."""
    global _MODELS_CACHE
    if _MODELS_CACHE is None:
        config_path = Path(__file__).parent / 'tool_calling_models.json'
        with open(config_path, 'r', encoding='utf-8') as f:
            _MODELS_CACHE = json.load(f)
    return _MODELS_CACHE


def supports_official_tool_calling(model: str) -> bool:
    """Check if a model supports official tool calling.
    
    Args:
        model: The model identifier (e.g., "OpenRouter/openai/gpt-4o")
        
    Returns:
        True if the model supports official tool calling, False otherwise.
    """
    config = _load_models_config()
    
    # Normalize model name - ensure it starts with OpenRouter/
    normalized = model
    if not normalized.startswith('OpenRouter/'):
        # Try common prefixes
        if normalized.startswith('openrouter/'):
            normalized = 'OpenRouter/' + normalized[len('openrouter/'):]
        elif '/' in normalized:
            normalized = 'OpenRouter/' + normalized
    
    # Check exact match
    if normalized in config.get('official_tool_calling_models', []):
        return True
    
    # Check wildcard patterns
    for pattern in config.get('wildcard_patterns', []):
        if fnmatch.fnmatch(normalized, pattern):
            return True
    
    return False


def normalize_model_name(model: str) -> str:
    """Normalize a model name to the canonical form used in the config.
    
    Handles variations like:
    - "openrouter/openai/gpt-4o" -> "OpenRouter/openai/gpt-4o"
    - "OpenRouter/openai/gpt-4o" -> "OpenRouter/openai/gpt-4o"
    - "openai/gpt-4o" -> "OpenRouter/openai/gpt-4o"
    """
    normalized = model.strip()
    
    # Remove leading/trailing whitespace and handle case variations
    lower = normalized.lower()
    if lower.startswith('openrouter/'):
        normalized = 'OpenRouter/' + normalized[len('openrouter/'):]
    elif '/' in normalized and not lower.startswith('openrouter'):
        # Assume it's a provider/model format without OpenRouter prefix
        normalized = 'OpenRouter/' + normalized
    
    return normalized