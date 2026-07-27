"""Drop-in replacement for the reference ``_parse_tool_param`` dispatch loop.

The parser in the task description does::

    options   = content.split('<tool_call>', 1)[-1].rsplit('</tool_call>', 1)[0].strip()
    tool_name = _parse_tool_param(options, 'tool_name')
    match tool_name: ...

which fails on every malformed shape in the corpus.  Swap it for
:func:`dispatch` and keep the rest of your agent unchanged.

    from toolcall_repair.adapter import dispatch

    result = dispatch(model_output, handlers={
        'bash':            tool_bash,
        'read_file':       tool_read_file,
        'write_to_file':   tool_write_to_file,
        'replace_in_file': tool_replace_in_file,
        'done':            tool_done,
    })
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from .core import ToolCall, ToolCallRepairer
from .schema import DEFAULT_REGISTRY, ToolRegistry

__all__ = ["parse_tool_param", "dispatch", "dispatch_all"]


def parse_tool_param(options: str, param_name: str, default_value: str = "") -> str:
    """Fault-tolerant version of the reference ``_parse_tool_param``.

    Same signature and return type, but recovers the value even when the tags
    are broken, duplicated, wrapper-style or missing entirely.
    """
    call = ToolCallRepairer(DEFAULT_REGISTRY).parse(options)
    if call is None:
        return default_value
    if param_name in {"tool_name", "name"}:
        return call.name or default_value
    value = call.params.get(param_name)
    return default_value if value in (None, "") else value


def dispatch(
    content: str,
    handlers: Dict[str, Callable[..., Any]],
    *,
    registry: ToolRegistry = DEFAULT_REGISTRY,
    on_error: Optional[Callable[[ToolCall], Any]] = None,
    strict: bool = False,
) -> Any:
    """Repair the first tool call in *content* and invoke its handler.

    ``handlers`` maps tool name -> callable; the callable receives the tool's
    typed keyword arguments (``ToolCall.kwargs``).

    ``strict`` refuses calls with missing required parameters.  ``on_error`` is
    called with the :class:`ToolCall` instead of raising.
    """
    results = dispatch_all(content, handlers, registry=registry, on_error=on_error,
                           strict=strict, first_only=True)
    return results[0] if results else None


def dispatch_all(
    content: str,
    handlers: Dict[str, Callable[..., Any]],
    *,
    registry: ToolRegistry = DEFAULT_REGISTRY,
    on_error: Optional[Callable[[ToolCall], Any]] = None,
    strict: bool = False,
    first_only: bool = False,
) -> List[Any]:
    """Repair and dispatch every tool call in *content*."""
    calls = ToolCallRepairer(registry).parse_all(content)
    if first_only:
        calls = calls[:1]

    out: List[Any] = []
    for call in calls:
        problem = None
        if call.name is None or call.name not in handlers:
            problem = f"Unknown tool: {call.name}"
        elif strict and call.missing:
            problem = f"Missing required parameter(s) for {call.name}: {', '.join(call.missing)}"

        if problem is not None:
            if on_error is not None:
                out.append(on_error(call))
            else:
                out.append(problem)
            continue
        out.append(handlers[call.name](**call.kwargs))
    return out
