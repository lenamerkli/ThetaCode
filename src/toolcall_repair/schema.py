"""Tool schema definitions.

The repairer is *schema-driven*: knowing which parameter names are legal for a
given tool is what lets us tell a real structural tag (``</command>``) apart
from a tag-like fragment that happens to live inside a payload
(``HttpEvent<BatchUploadResponse>``, ``grep -o '<node[^>]*'``).

Nothing here is specific to a particular agent: build your own
:class:`ToolRegistry` for your own tool set.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

__all__ = [
    "ParamSpec",
    "ToolSpec",
    "ToolRegistry",
    "DEFAULT_REGISTRY",
    "GENERIC_TAGS",
    "TOOL_CALL_TAGS",
    "CONTAINER_TAGS",
    "TOOL_NAME_TAGS",
    "PARAM_WRAPPER_TAGS",
]

# Tags that wrap a whole call.
TOOL_CALL_TAGS = frozenset({"tool_call", "toolcall", "invoke", "function_call", "tool_use"})
# Plural/batch containers that wrap *calls* rather than being one; they carry no
# data of their own and are dissolved during segmentation.
CONTAINER_TAGS = frozenset({"tool_calls", "toolcalls", "function_calls", "tool_uses", "invokes"})
# Tags whose *content* is the name of the tool.
TOOL_NAME_TAGS = frozenset({"tool_name", "toolname", "name", "function", "tool"})
# Generic wrapper tags used instead of the parameter's own name, e.g.
# ``<parameter name="path">`` or the stray ``</arg_value>`` closers models emit.
PARAM_WRAPPER_TAGS = frozenset({"parameter", "param", "arg", "argument", "arg_value", "arg_key", "value"})

GENERIC_TAGS = TOOL_CALL_TAGS | CONTAINER_TAGS | TOOL_NAME_TAGS | PARAM_WRAPPER_TAGS


def _to_int(value: str) -> int:
    return int(str(value).strip())


def _to_bool(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ParamSpec:
    """A single parameter of a tool."""

    name: str
    required: bool = False
    default: Any = ""
    #: Callable applied to the raw string when building typed kwargs.
    coerce: Optional[Callable[[str], Any]] = None
    #: Alternative spellings a model might emit for this parameter.
    aliases: Sequence[str] = ()
    #: ``True`` for free-form payloads (code, diffs, shell) whose content must
    #: never be escaped, re-indented or otherwise touched.
    verbatim: bool = True

    def cast(self, raw: str) -> Any:
        if self.coerce is None:
            return raw
        try:
            return self.coerce(raw)
        except (TypeError, ValueError):
            return self.default


@dataclass(frozen=True)
class ToolSpec:
    """A tool: its name plus its parameters, in canonical order."""

    name: str
    params: Sequence[ParamSpec] = field(default_factory=tuple)
    aliases: Sequence[str] = ()

    @property
    def param_names(self) -> List[str]:
        return [p.name for p in self.params]

    def param(self, name: str) -> Optional[ParamSpec]:
        lowered = name.lower()
        for spec in self.params:
            if spec.name.lower() == lowered or lowered in {a.lower() for a in spec.aliases}:
                return spec
        return None

    def resolve_param(self, name: str) -> Optional[str]:
        """Map an emitted parameter name (or alias) onto its canonical name."""
        spec = self.param(name)
        return spec.name if spec else None

    def kwargs(self, values: Dict[str, str]) -> Dict[str, Any]:
        """Build a typed kwargs dict, filling in defaults for absent params."""
        out: Dict[str, Any] = {}
        for spec in self.params:
            if spec.name in values and str(values[spec.name]) != "":
                out[spec.name] = spec.cast(values[spec.name])
            else:
                out[spec.name] = spec.default
        return out

    def missing_required(self, values: Dict[str, str]) -> List[str]:
        return [p.name for p in self.params if p.required and not values.get(p.name)]


class ToolRegistry:
    """A collection of :class:`ToolSpec` with fuzzy lookup."""

    def __init__(self, tools: Iterable[ToolSpec] = ()):
        self._tools: Dict[str, ToolSpec] = {}
        for tool in tools:
            self.add(tool)

    def add(self, tool: ToolSpec) -> "ToolRegistry":
        self._tools[tool.name.lower()] = tool
        return self

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self.get(name) is not None

    def __iter__(self):
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)

    @property
    def names(self) -> List[str]:
        return [t.name for t in self._tools.values()]

    def get(self, name: Optional[str]) -> Optional[ToolSpec]:
        if not name:
            return None
        key = name.strip().lower()
        if key in self._tools:
            return self._tools[key]
        for tool in self._tools.values():
            if key in {a.lower() for a in tool.aliases}:
                return tool
        return None

    def fuzzy_get(self, name: Optional[str], cutoff: float = 0.72) -> Optional[ToolSpec]:
        """Exact lookup, else closest match (handles ``read_fil``/``readfile``)."""
        exact = self.get(name)
        if exact is not None:
            return exact
        if not name:
            return None
        key = name.strip().lower()
        if not key or len(key) > 64:
            return None
        squashed = {t.name.lower().replace("_", ""): t for t in self._tools.values()}
        if key.replace("_", "") in squashed:
            return squashed[key.replace("_", "")]
        match = difflib.get_close_matches(key, list(self._tools), n=1, cutoff=cutoff)
        return self._tools[match[0]] if match else None

    def infer_from_params(self, param_names: Iterable[str]) -> Optional[ToolSpec]:
        """Guess the tool from the set of parameter names that were emitted."""
        seen = {p.lower() for p in param_names if p}
        if not seen:
            return None
        best, best_score = None, 0.0
        for tool in self._tools.values():
            known = {p.name.lower() for p in tool.params}
            known |= {a.lower() for p in tool.params for a in p.aliases}
            if not known:
                continue
            overlap = len(seen & known)
            if not overlap:
                continue
            # Reward coverage of what we saw, lightly penalise unused params.
            score = overlap / len(seen) + 0.25 * (overlap / len(known))
            if score > best_score:
                best, best_score = tool, score
        return best if best_score >= 0.5 else None


#: Registry matching the reference agent parser in the task description.
DEFAULT_REGISTRY = ToolRegistry(
    [
        ToolSpec(
            "read_file",
            [
                ParamSpec("path", required=True, verbatim=False),
                ParamSpec("start_line", default=1, coerce=_to_int, verbatim=False),
                ParamSpec("end_line", default=1000, coerce=_to_int, verbatim=False),
                ParamSpec("start_char", default=0, coerce=_to_int, verbatim=False),
                ParamSpec("end_char", default=100000, coerce=_to_int, verbatim=False),
                ParamSpec("max_chars", default=1000000, coerce=_to_int, verbatim=False),
            ],
            aliases=("read", "readfile", "view_file"),
        ),
        ToolSpec(
            "write_to_file",
            [
                ParamSpec("path", required=True, verbatim=False),
                ParamSpec("content", required=True),
            ],
            aliases=("write", "write_file", "create_file"),
        ),
        ToolSpec(
            "replace_in_file",
            [
                ParamSpec("path", required=True, verbatim=False),
                ParamSpec("search", required=True),
                ParamSpec("replace", required=True),
            ],
            aliases=("replace", "edit_file", "str_replace"),
        ),
        ToolSpec(
            "bash",
            [
                ParamSpec("command", required=True),
                ParamSpec("timeout", default=60, coerce=_to_int, verbatim=False),
                ParamSpec("directory", default="/home/agent/", verbatim=False, aliases=("cwd", "dir")),
                ParamSpec("venv", default="", verbatim=False),
                ParamSpec("max_chars", default=100000, coerce=_to_int, verbatim=False),
            ],
            aliases=("shell", "run", "execute_command", "run_command"),
        ),
        ToolSpec(
            "ask_user",
            [ParamSpec("question", required=True)],
            aliases=("ask", "ask_followup_question"),
        ),
        ToolSpec(
            "done",
            [
                ParamSpec("status", default="", verbatim=False),
                ParamSpec("summary", default=""),
            ],
            aliases=("finish", "complete", "attempt_completion"),
        ),
    ]
)
