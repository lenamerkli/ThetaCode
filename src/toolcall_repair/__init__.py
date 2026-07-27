"""toolcall_repair - repair malformed XML-style tool calls from LLMs.

Quick start
-----------
    from toolcall_repair import repair, parse

    print(repair(broken_text))      # canonical <tool_call> XML
    call = parse(broken_text)       # -> ToolCall
    call.name                       # 'bash'
    call.params['command']          # verbatim payload
    call.kwargs                     # typed + defaults filled
    call.warnings                   # what had to be fixed

Custom tool sets
----------------
    from toolcall_repair import ParamSpec, ToolSpec, ToolRegistry, ToolCallRepairer

    reg = ToolRegistry([ToolSpec("grep", [ParamSpec("pattern", required=True)])])
    ToolCallRepairer(reg).parse(text)
"""

from .core import (
    ToolCall,
    ToolCallRepairer,
    find_tool_calls,
    parse,
    parse_all,
    repair,
)
from .schema import (
    DEFAULT_REGISTRY,
    ParamSpec,
    ToolRegistry,
    ToolSpec,
)

__version__ = "1.0.0"

__all__ = [
    "ToolCall",
    "ToolCallRepairer",
    "repair",
    "parse",
    "parse_all",
    "find_tool_calls",
    "ParamSpec",
    "ToolSpec",
    "ToolRegistry",
    "DEFAULT_REGISTRY",
    "__version__",
]

from .adapter import dispatch, dispatch_all, parse_tool_param  # noqa: E402

__all__ += ["dispatch", "dispatch_all", "parse_tool_param"]
