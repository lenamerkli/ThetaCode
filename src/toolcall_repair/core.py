"""Recovery of malformed XML-ish tool calls emitted by LLMs.

Why not a generic XML repairer?
-------------------------------
Libraries such as ``fix-llm-xml``, ``sloppy-xml-py`` or ``json_repair`` assume
the payload is *data* that may be escaped, re-indented or re-serialised.  Tool
calls are different: a parameter value is frequently source code, a shell
command or a unified diff that legitimately contains tag-like text::

    grep -o '<node[^>]*'
    this.http.post<BatchUploadResponse>(...)
    # Output format: "<hash> <path>"

Feeding that to an XML parser either explodes or silently corrupts the payload.

This module therefore treats the input as *text with markers* and uses the tool
schema to decide which markers are structural.  Concretely:

* a ``<foo>`` is only a parameter opener when ``foo`` is a declared parameter of
  the resolved tool **and** it sits at a structurally plausible position
  (line start, or immediately after a previous tag);
* a ``</foo>`` only closes a value when ``foo`` is a declared parameter, the
  tool name, or a known generic wrapper (``parameter``/``arg_value``/...);
* everything between those markers is copied out **byte for byte**.

Recognised corruptions
----------------------
=====================================  ====================================
Corruption                              Handling
=====================================  ====================================
``<tool_call>bash</tool_name>``         bare leading tool name + orphan close
``<tool_call name="bash">``             name taken from an attribute
repeated ``<tool_call>`` openers        collapsed
``</parameter>`` x16 tag storms         ignored
truncated final tag (``</command``)     ignored
``</arg_value>replace>``                close + degenerate reopen
``/home/agentcommand>``                 trailing degenerate closer stripped
``<parameter name="path" string="1">``  wrapper form + junk attributes
``<search>``...``<search>``             duplicate name remapped to ``replace``
missing ``</tool_call>``                implied at end of text
mismatched close (``<replace>..</search>``)  accepted as a boundary
=====================================  ====================================
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .schema import (
    CONTAINER_TAGS,
    GENERIC_TAGS,
    PARAM_WRAPPER_TAGS,
    TOOL_CALL_TAGS,
    TOOL_NAME_TAGS,
    DEFAULT_REGISTRY,
    ToolRegistry,
    ToolSpec,
)

__all__ = ["ToolCall", "ToolCallRepairer", "repair", "parse", "parse_all", "find_tool_calls"]

_NAME = r"[A-Za-z_][A-Za-z0-9_.:-]*"
#: Bars used by model special tokens: ASCII ``|`` and fullwidth ``｜`` (U+FF5C),
#: as in DeepSeek's ``<｜tool▁calls▁begin｜>`` or Llama's ``<|python_tag|>``.
_BARS = "\uff5c|"
#: A leaked special-token marker sitting inside a tag, e.g. ``｜DSML｜``.
_SPECIAL = rf"[{_BARS}][^<>{_BARS}]{{0,40}}[{_BARS}]"
#: ``<｜DSML｜command …>`` / ``</｜DSML｜parameter>`` / ``</｜DSML｜>``
_SPECIAL_TAG = re.compile(rf"<\s*(?P<slash>/?)\s*(?:{_SPECIAL})\s*(?P<name>{_NAME})?(?P<attrs>\s[^<>]*)?\s*/?>")
#: A marker fragment that never reaches a ``>``: ``</｜DSML｜</command>``
_SPECIAL_FRAGMENT = re.compile(rf"<\s*/?\s*(?:{_SPECIAL})(?![^<>]*>)")
_TOOL_CALL_OPEN = re.compile(rf"<\s*(?P<tag>{_NAME})(?P<attrs>\s[^>]*)?/?>", re.S)
_ATTR = re.compile(rf"(?P<key>{_NAME})\s*=\s*(?P<q>[\"'])(?P<val>.*?)(?P=q)", re.S)


def _attrs(text: Optional[str]) -> Dict[str, str]:
    if not text:
        return {}
    return {m.group("key").lower(): m.group("val") for m in _ATTR.finditer(text)}


@dataclass
class ToolCall:
    """A recovered tool call."""

    name: Optional[str]
    params: Dict[str, str] = field(default_factory=dict)
    order: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    spec: Optional[ToolSpec] = None
    raw: str = ""
    span: Tuple[int, int] = (0, 0)

    # -- quality ---------------------------------------------------------
    @property
    def ok(self) -> bool:
        """True when a tool was identified and all required params are present."""
        return self.spec is not None and not self.missing

    @property
    def missing(self) -> List[str]:
        if self.spec is None:
            return []
        return self.spec.missing_required(self.params)

    @property
    def confidence(self) -> float:
        score = 1.0
        if self.spec is None:
            score -= 0.5
        score -= 0.25 * len(self.missing)
        score -= 0.05 * len(self.warnings)
        return max(0.0, min(1.0, score))

    # -- consumption -----------------------------------------------------
    @property
    def kwargs(self) -> Dict[str, Any]:
        """Typed kwargs (ints coerced, defaults filled) ready for dispatch."""
        if self.spec is None:
            return dict(self.params)
        return self.spec.kwargs(self.params)

    def get(self, name: str, default: Any = "") -> Any:
        return self.params.get(name, default)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "params": dict(self.params),
            "order": list(self.order),
            "warnings": list(self.warnings),
            "confidence": round(self.confidence, 3),
            "ok": self.ok,
        }

    # -- rendering -------------------------------------------------------
    def to_xml(self, indent: str = "") -> str:
        """Render the canonical, well-formed ``<tool_call>`` block."""
        lines = [f"{indent}<tool_call>"]
        if self.name:
            lines.append(f"{indent}<tool_name>{self.name}</tool_name>")
        for key in self.order:
            lines.append(f"{indent}<{key}>{self.params[key]}</{key}>")
        lines.append(f"{indent}</tool_call>")
        return "\n".join(lines)

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.to_xml()


class ToolCallRepairer:
    """Repairs malformed tool calls against a :class:`ToolRegistry`."""

    def __init__(self, registry: ToolRegistry = DEFAULT_REGISTRY, *, fuzzy: bool = True):
        self.registry = registry
        self.fuzzy = fuzzy
        self._blanked: Optional[Tuple[str, str]] = None

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def parse(self, text: str) -> Optional[ToolCall]:
        """Repair and return the first tool call found in *text*."""
        calls = self.parse_all(text)
        return calls[0] if calls else None

    def parse_all(self, text: str) -> List[ToolCall]:
        """Repair every tool call found in *text*."""
        return [self._parse_segment(seg, off) for seg, off in self._segments(text)]

    def repair(self, text: str, *, multi: bool = False) -> str:
        """Return *text* with tool calls rewritten in canonical form.

        By default only the **first** call is kept, matching single-call agent
        harnesses (the reference dispatcher reads one call per turn, so trailing
        calls are silently ignored anyway).  Dropping them is also the safer
        reading: a model that emits ``ask_user`` and then a build command must
        not have the build executed while it waits for an answer.

        Pass ``multi=True`` to canonicalise every call and keep surrounding prose.
        """
        calls = self.parse_all(text)
        if not calls:
            return text

        # Splice against the dissolved text so a container tag around the call
        # is not re-emitted verbatim in the surrounding context.
        canvas = self._dissolve_containers(self._strip_special_tokens(text))

        if not multi:
            first = calls[0]
            return (canvas[: first.span[0]] + first.to_xml()).strip()

        out, cursor = [], 0
        for call in calls:
            start, end = call.span
            out.append(canvas[cursor:start])
            out.append(call.to_xml())
            cursor = end
        out.append(canvas[cursor:])
        return "".join(out).strip()

    # ------------------------------------------------------------------
    # segmentation: locate each <tool_call> ... </tool_call> region
    # ------------------------------------------------------------------
    @staticmethod
    def _quoted(text: str, pos: int) -> bool:
        """True when *pos* sits inside a quoted string on its own line.

        Genuine leaked markup is emitted as bare markup; a marker appearing
        inside quotes -- ``echo '</|DSML|parameter>'`` -- is payload data.
        """
        line_start = text.rfind("\n", 0, pos) + 1
        before = text[line_start:pos]
        for q in ("'", '"', "`"):
            if before.count(q) % 2 == 1:
                return True
        # Inside an unterminated ``` fence -> payload (documentation, snippets).
        return text.count("```", 0, line_start) % 2 == 1

    @staticmethod
    def _marker_of(fragment: str) -> str:
        """Extract the bar-delimited marker token from a tag fragment."""
        m = re.search(rf"[{_BARS}][^<>{_BARS}]{{0,40}}[{_BARS}]", fragment)
        return m.group(0).strip(_BARS).strip().lower() if m else ""

    @classmethod
    def _marker_stats(cls, text: str) -> Dict[str, Dict[str, int]]:
        """Count occurrences of each bar-delimited marker inside tags."""
        stats: Dict[str, Dict[str, int]] = {}
        for m in _SPECIAL_TAG.finditer(text):
            tok = cls._marker_of(m.group(0))
            if not tok:
                continue
            s = stats.setdefault(tok, {"total": 0, "named": 0, "structural": 0})
            s["total"] += 1
            if m.group("name") and not cls._quoted(text, m.start()):
                s["named"] += 1
                if cls._structural(text, m.start()):
                    s["structural"] += 1
        return stats

    @classmethod
    def _strip_special_tokens(cls, text: str) -> str:
        """Remove leaked model special tokens from *tag names*.

        Some models emit their internal markup tokens inside tag names::

            <｜DSML｜tool_call>  <｜DSML｜command>ls</｜DSML｜parameter>  </｜DSML｜>

        The marker is delimited by ASCII ``|`` or fullwidth ``｜`` (U+FF5C).
        Because ``_NAME`` requires an ASCII-letter start, such tags are
        otherwise invisible to the scanner and the whole call is lost.

        Rewrites are confined to text that is *already* inside a tag (between
        ``<`` and ``>``), so a bar character in a shell pipeline or a payload is
        never touched.  Three shapes:

        ``<｜X｜name>``  -> ``<name>``      (named: keep the name)
        ``</｜X｜>``     -> ``</>``         (anonymous closer: name unknown)
        ``</｜X｜``      -> ``''``          (truncated fragment: drop)
        """
        if not any(b in text for b in _BARS):
            return text

        # Identify which marker is genuine leaked markup rather than incidental
        # payload text.  A real leak is a *systematic* artefact of the decoder:
        # the same token recurs, and it attaches to structural tag names.  A
        # payload occurrence such as ``TOK = '<|endoftext|>'`` appears once and
        # carries no tag name, so it is left untouched.
        # A named bar-delimited tag (``</｜DSML｜parameter>``) is already strong
        # evidence: a payload occurrence carries no tag name.  Requiring a name
        # is what protects literals like ``'<|endoftext|>'``.
        leaked = {tok for tok, hits in cls._marker_stats(text).items() if hits["named"] >= 1}
        if not leaked:
            return text

        def fix(m: "re.Match") -> str:
            whole = m.group(0)
            name = m.group("name")
            if cls._marker_of(whole) not in leaked or cls._quoted(text, m.start()):
                return whole
            if not name:
                # Nameless closer: keep it as an anonymous boundary marker so
                # the value it terminates is still bounded correctly.
                return "</>" if m.group("slash") else ""
            return f"<{m.group('slash')}{name}{m.group('attrs') or ''}>"

        stripped = _SPECIAL_TAG.sub(fix, text)

        def drop(m: "re.Match") -> str:
            if cls._marker_of(m.group(0)) not in leaked or cls._quoted(stripped, m.start()):
                return m.group(0)
            return ""

        return _SPECIAL_FRAGMENT.sub(drop, stripped)

    @staticmethod
    def _dissolve_containers(text: str) -> str:
        """Blank out plural batch containers, preserving every offset.

        ``<tool_calls>`` wraps calls rather than being one, so it must not open a
        segment nor terminate the call inside it.  Replacing each tag with an
        equal run of spaces keeps all indices valid for splicing.
        """
        def blank(m: "re.Match") -> str:
            # Only dissolve at a structural position (line start / right after a
            # tag).  A literal ``<tool_calls>`` inside a shell command or code
            # payload must survive byte-for-byte.
            if not ToolCallRepairer._structural(text, m.start()):
                return m.group(0)
            return " " * len(m.group(0))

        return re.sub(r"</?\s*(?:%s)(?:\s[^>]*)?/?>" % "|".join(CONTAINER_TAGS), blank, text)

    def _segments(self, text: str) -> List[Tuple[str, int]]:
        text = self._strip_special_tokens(text)
        original = text
        text = self._dissolve_containers(text)
        self._blanked = (original, text) if text != original else None

        opens = [
            m for m in _TOOL_CALL_OPEN.finditer(text)
            if m.group("tag").lower() in TOOL_CALL_TAGS and not m.group(0).startswith("</")
        ]
        if not opens:
            # Tolerate a call that lost its wrapper entirely.
            return [(text, 0)] if self._looks_like_call(text) else []

        closes = [m.start() for m in re.finditer(r"</\s*(?:%s)\s*>" % "|".join(TOOL_CALL_TAGS), text)]
        segments: List[Tuple[str, int]] = []
        self._spans = getattr(self, "_spans", {})
        i = 0
        while i < len(opens):
            # Merge a run of openers into one call.  Two shapes merge:
            #   * adjacent openers      -> <tool_call>\n<tool_call name="bash">
            #   * a *restart* opener    -> <tool_call>read_file\n<path>..</path>
            #                              <tool_call>read_file</tool_name>...
            # A restart is an opener with no </tool_call> between it and the
            # previous one: the model re-announced the same call instead of
            # closing it, so the two share a single logical body.
            j = i
            while j + 1 < len(opens):
                gap_start, gap_end = opens[j].end(), opens[j + 1].start()
                if any(gap_start <= c < gap_end for c in closes):
                    break  # properly closed in between -> a genuinely new call
                j += 1

            start = opens[i].start()
            nxt = opens[j + 1].start() if j + 1 < len(opens) else len(text)
            close = next((c for c in closes if c >= opens[j].end()), None)
            if close is not None and close < nxt:
                end, body_end = close + text[close:].index(">") + 1, close
            else:
                end = body_end = nxt

            # Body = every opener's attributes + all inter-opener content, so a
            # restart keeps the parameters stated before it.
            chunks = [opens[i].group(0)]
            for k in range(i, j + 1):
                chunks.append(text[opens[k].end():(opens[k + 1].start() if k < j else body_end)])
                if k < j:
                    chunks.append(opens[k + 1].group(0))
            segments.append(("".join(chunks), start))
            self._spans[start] = end
            i = j + 1
        return segments

    def _looks_like_call(self, text: str) -> bool:
        for tool in self.registry:
            for p in tool.params:
                if re.search(rf"<\s*{re.escape(p.name)}\s*>", text):
                    return True
        return bool(re.search(r"<\s*(?:%s)\b" % "|".join(TOOL_NAME_TAGS | PARAM_WRAPPER_TAGS), text))

    # ------------------------------------------------------------------
    # a single call
    # ------------------------------------------------------------------
    def _restore(self, value: str) -> str:
        """Undo container blanking inside a recovered parameter value.

        Dissolution happens before we know where payload boundaries are, so a
        container tag sitting at a line start *within* a value gets blanked.
        Once the value is delimited we know it is payload, and restore it.
        """
        if self._blanked is None or not value.strip(" "):
            return value
        original, dissolved = self._blanked
        idx = dissolved.find(value)
        if idx == -1 or dissolved.find(value, idx + 1) != -1:
            return value  # absent or ambiguous -> leave as-is
        return original[idx:idx + len(value)]

    def _parse_segment(self, segment: str, offset: int) -> ToolCall:
        warnings: List[str] = []
        body, name = self._extract_name(segment, warnings)
        spec = self.registry.get(name)
        if spec is None and name and self.fuzzy:
            spec = self.registry.fuzzy_get(name)
            if spec is not None:
                warnings.append(f"tool name {name!r} fuzzy-matched to {spec.name!r}")

        if spec is None:
            # Last resort: identify the tool from the parameter names present.
            guessed = self.registry.infer_from_params(self._candidate_param_names(body))
            if guessed is not None:
                spec = guessed
                warnings.append(
                    f"tool name {name!r} unresolved; inferred {spec.name!r} from parameters"
                    if name else f"no tool name found; inferred {spec.name!r} from parameters"
                )

        params, order, pwarn = self._extract_params(body, spec)
        warnings.extend(pwarn)

        call = ToolCall(
            name=spec.name if spec else (name or None),
            params=params,
            order=order,
            warnings=warnings,
            spec=spec,
            raw=segment,
            span=(offset, getattr(self, "_spans", {}).get(offset, offset + len(segment))),
        )
        if spec is not None:
            for miss in call.missing:
                warnings.append(f"required parameter {miss!r} missing")
        return call

    # -- tool name -------------------------------------------------------
    def _extract_name(self, segment: str, warnings: List[str]) -> Tuple[str, Optional[str]]:
        """Return ``(body_without_name_markup, tool_name)``."""
        name: Optional[str] = None
        body = segment

        # 1. attributes on the wrapper(s): <tool_call name="bash">
        head_end = 0
        for m in _TOOL_CALL_OPEN.finditer(segment):
            if m.group("tag").lower() not in TOOL_CALL_TAGS or m.start() != head_end:
                break
            head_end = m.end()
            attr = _attrs(m.group("attrs"))
            name = name or attr.get("name") or attr.get("tool") or attr.get("tool_name")
        if head_end:
            body = segment[head_end:]
            if head_end > len(_TOOL_CALL_OPEN.match(segment).group(0) if _TOOL_CALL_OPEN.match(segment) else ""):
                pass

        # 2. a real <tool_name> element (content wins over its attributes)
        full = re.search(
            rf"<\s*(?P<tag>{_NAME})(?P<attrs>\s[^>]*)?>(?P<inner>.*?)</\s*(?P=tag)\s*>",
            body,
            re.S,
        )
        if full and full.group("tag").lower() in TOOL_NAME_TAGS and len(full.group("inner")) < 200:
            inner = full.group("inner").strip()
            attr = _attrs(full.group("attrs"))
            name = inner or attr.get("name") or name
            body = body[: full.start()] + body[full.end():]
        else:
            # 2b. self-contained opener only: <tool_name name="read_file">
            solo = re.search(rf"<\s*(?P<tag>{_NAME})(?P<attrs>\s[^>]*)>", body)
            if solo and solo.group("tag").lower() in TOOL_NAME_TAGS:
                attr = _attrs(solo.group("attrs"))
                if attr.get("name"):
                    name = name or attr["name"]
                    body = body[: solo.start()] + body[solo.end():]

        # 3. bare text before the first tag: <tool_call>bash</tool_name>
        #    Strip it even when the name is already known: a model that repeats
        #    the name inline (``<tool_call>read_file<tool_name>read_file...``)
        #    would otherwise leave stray text that makes the next opener look
        #    non-structural, silently dropping that parameter.
        bare = re.match(rf"\s*(?P<n>{_NAME})\s*(?=<|\n|$)", body)
        if bare:
            candidate = bare.group("n")
            known = self.registry.get(candidate) or (
                self.fuzzy and self.registry.fuzzy_get(candidate, cutoff=0.85)
            )
            matches_known_name = bool(name) and candidate.lower() == name.lower()
            if known or matches_known_name:
                body = body[bare.end():]
                if not name:
                    name = candidate
                    warnings.append("tool name recovered from bare text after <tool_call>")
                else:
                    warnings.append("removed duplicate inline tool name")

        # 4. sweep away orphan </tool_name> / <tool_name> leftovers
        cleaned = re.sub(rf"</?\s*(?:{'|'.join(TOOL_NAME_TAGS)})(?:\s[^>]*)?/?>", "", body)
        if cleaned != body:
            body = cleaned
        return body, (name.strip() if name else None)

    # -- parameters ------------------------------------------------------
    def _candidate_param_names(self, body: str) -> List[str]:
        found: List[str] = []
        for m in re.finditer(rf"<\s*/?\s*({_NAME})(\s[^>]*)?>", body):
            tag, attrs = m.group(1).lower(), _attrs(m.group(2))
            if tag in PARAM_WRAPPER_TAGS and attrs.get("name"):
                found.append(attrs["name"])
            elif tag not in GENERIC_TAGS:
                found.append(tag)
        return found

    def _marker_names(self, spec: Optional[ToolSpec], body: str = "") -> Tuple[set, set]:
        """(names usable as openers, names usable as closers)."""
        openers = set(GENERIC_TAGS)
        closers = set(GENERIC_TAGS)
        if spec is not None:
            for p in spec.params:
                openers.add(p.name.lower())
                openers.update(a.lower() for a in p.aliases)
            closers |= openers
            closers.add(spec.name.lower())
            # Accept near-miss spellings as closers too, so ``<questions>...
            # </questions>`` is bounded correctly before being remapped to
            # ``question``.  Restricted to tags actually present in the body.
            declared = [p.name for p in spec.params]
            for m in re.finditer(rf"</\s*({_NAME})\s*>", body):
                tag = m.group(1).lower()
                if tag not in closers and difflib.get_close_matches(tag, declared, n=1, cutoff=0.8):
                    closers.add(tag)
        else:
            # Unknown tool: we have no schema to lean on, so fall back to
            # structure.  Any tag that is opened at a structural position and
            # later properly closed is taken to be a parameter.  Without this an
            # unregistered tool would lose every argument.
            for m in re.finditer(rf"<\s*({_NAME})(?:\s[^>]*)?>", body):
                tag = m.group(1).lower()
                if tag in GENERIC_TAGS or not self._structural(body, m.start()):
                    continue
                if re.search(rf"</\s*{re.escape(tag)}\s*>", body[m.end():]):
                    openers.add(tag)
                    closers.add(tag)
        return openers, closers

    def _extract_params(
        self, body: str, spec: Optional[ToolSpec]
    ) -> Tuple[Dict[str, str], List[str], List[str]]:
        warnings: List[str] = []
        opener_names, closer_names = self._marker_names(spec, body)
        openers = self._find_openers(body, opener_names, spec)
        closers = self._find_closers(body, closer_names)

        params: Dict[str, str] = {}
        order: List[str] = []
        pos = 0
        for idx, op in enumerate(openers):
            if op["start"] < pos:
                continue  # swallowed by a previous value
            value_start = op["end"]
            # terminator = earliest accepted closer or next accepted opener
            nxt_open = next((o["start"] for o in openers[idx + 1:] if o["start"] >= value_start), len(body))
            nxt_close = next(((c[0], c[1]) for c in closers if c[0] >= value_start), None)
            if nxt_close and nxt_close[0] <= nxt_open:
                value_end, pos = nxt_close[0], nxt_close[1]
            else:
                value_end = pos = nxt_open
                if nxt_open < len(body):
                    warnings.append(f"no closing tag for <{op['name']}>; ended at next parameter")
                else:
                    warnings.append(f"no closing tag for <{op['name']}>; ended at end of call")

            raw_probe = self._restore(body[value_start:value_end])
            raw = raw_probe
            if op["wrapper"] is None:
                # Degenerate reopen (``</arg_value>replace>``): the newline that
                # follows is the structural break that canonically sits between
                # the closer and the opener, not part of the payload.
                raw = re.sub(r"\A\r?\n", "", raw, count=1)
            name, note = self._resolve_name(op, spec, params, order, raw_probe)
            if note:
                warnings.append(note)
            if name is None:
                continue
            pspec = spec.param(name) if spec else None
            value = self._clean_value(raw, pspec, opener_names | closer_names, warnings)
            if name in params:
                if not value.strip():
                    continue  # empty duplicate: drop silently
                warnings.append(f"duplicate parameter {name!r}; keeping the later value")
                params[name] = value
                continue
            params[name] = value
            order.append(name)
        return params, order, warnings

    def _find_openers(self, body: str, names: set, spec: Optional[ToolSpec]) -> List[Dict[str, Any]]:
        found: List[Dict[str, Any]] = []
        for m in re.finditer(rf"<\s*({_NAME})((?:\s[^>]*)?)/?>", body):
            tag = m.group(1).lower()
            attrs = _attrs(m.group(2))
            explicit = False
            if tag in PARAM_WRAPPER_TAGS:
                pname = attrs.get("name") or attrs.get("key")
                if not pname:
                    continue
                # ``<parameter name="x">`` is unambiguous markup: it is vanishingly
                # unlikely inside a payload, so accept it wherever it appears.
                explicit = True
            elif tag in names and tag not in TOOL_CALL_TAGS | CONTAINER_TAGS:
                pname = tag
            elif self._near_param(tag, spec, body, m):
                # Near-miss spelling of a declared parameter (``<questions>``
                # for ``question``).  Only accepted when properly closed, so a
                # stray tag inside a payload cannot hijack a slot.
                pname = tag
            else:
                continue
            if not explicit and not self._structural(body, m.start()):
                continue
            found.append({"start": m.start(), "end": m.end(), "name": pname, "wrapper": tag})

        # Degenerate reopen: ``</arg_value>replace>`` -> a ``replace`` opener.
        for m in re.finditer(rf"(?<=>)[ \t]*({_NAME})>", body):
            if m.group(1).lower() in names and not any(f["start"] <= m.start() < f["end"] for f in found):
                found.append({"start": m.start(), "end": m.end(), "name": m.group(1), "wrapper": None})

        found.sort(key=lambda d: d["start"])
        return found

    @staticmethod
    def _near_param(tag: str, spec: Optional[ToolSpec], body: str, m: "re.Match") -> bool:
        """True when *tag* is a misspelling of a declared parameter of *spec*."""
        if spec is None or tag in GENERIC_TAGS:
            return False
        if not re.search(rf"</\s*{re.escape(tag)}\s*>", body[m.end():]):
            return False  # unclosed -> almost certainly payload text
        return bool(difflib.get_close_matches(tag, [p.name for p in spec.params], n=1, cutoff=0.8))

    @staticmethod
    def _structural(body: str, start: int) -> bool:
        """An opener is structural at line start or immediately after a tag.

        This is what keeps ``"<hash> <path>"`` inside a code payload from being
        mistaken for a real ``<path>`` parameter.
        """
        if start == 0:
            return True
        if body[start - 1] == ">":
            return True
        line_head = body.rfind("\n", 0, start)
        return not body[line_head + 1:start].strip()

    @staticmethod
    def _find_closers(body: str, names: set) -> List[Tuple[int, int]]:
        out = []
        for m in re.finditer(rf"</\s*({_NAME})?(?:\s[^>]*)?>", body):
            tag = m.group(1)
            # ``</>`` is an anonymous closer produced by special-token
            # stripping: it bounds a value without naming it.
            if tag is None or tag.lower() in names:
                out.append((m.start(), m.end()))
        return out

    def _resolve_name(
        self,
        op: Dict[str, Any],
        spec: Optional[ToolSpec],
        params: Dict[str, str],
        order: List[str],
        raw_value: str = "",
    ) -> Tuple[Optional[str], Optional[str]]:
        raw_name = op["name"]
        if spec is None:
            return raw_name, None
        canon = spec.resolve_param(raw_name)
        if canon is None:
            match = difflib.get_close_matches(raw_name.lower(), [p.name for p in spec.params], n=1, cutoff=0.8)
            if match:
                return match[0], f"parameter {raw_name!r} corrected to {match[0]!r}"
            return raw_name, f"unknown parameter {raw_name!r} for tool {spec.name!r}"
        if canon in params:
            # A repeat whose value is identical is a *restatement* (the model
            # re-announced the call), not a new slot -> drop it.
            if params[canon].strip() == raw_value.strip():
                return None, None
            # ``<search>..</search><search>..`` -> the second one is ``replace``.
            nxt = next((p.name for p in spec.params if p.name not in params), None)
            if nxt is not None:
                return nxt, f"duplicate <{raw_name}>; remapped to {nxt!r} (next unfilled parameter)"
        return canon, None

    @staticmethod
    def _clean_value(raw: str, pspec, marker_names: set, warnings: List[str]) -> str:
        value = raw
        # Trailing degenerate closer that lost its ``</``.  Two shapes:
        #   ``...value\ncommand>``      -> token stands alone
        #   ``/home/agentcommand>``     -> token is glued onto the value
        m = re.search(rf"({_NAME})>[ \t]*\r?\n?\s*$", value)
        if m:
            token = m.group(1).lower()
            if token in marker_names:
                value = value[: m.start()]
                warnings.append(f"stripped degenerate closing tag '{token}>'")
            else:
                glued = next(
                    (n for n in sorted(marker_names, key=len, reverse=True)
                     if token.endswith(n) and token != n),
                    None,
                )
                if glued:
                    value = value[: m.start(1) + len(token) - len(glued)]
                    warnings.append(f"split closing tag '{glued}>' glued onto the value")
        if pspec is None or not pspec.verbatim:
            value = value.strip()
        if pspec is not None and pspec.verbatim and f"</{pspec.name}>" in value:
            warnings.append(f"value of {pspec.name!r} contains a literal </{pspec.name}>")
        return value


# ----------------------------------------------------------------------
# module-level conveniences
# ----------------------------------------------------------------------
_DEFAULT = ToolCallRepairer()


def repair(text: str, registry: ToolRegistry = DEFAULT_REGISTRY, *, multi: bool = False) -> str:
    """Rewrite the tool call in *text* into canonical, well-formed XML.

    Keeps only the first call unless ``multi=True``; see
    :meth:`ToolCallRepairer.repair`.
    """
    engine = _DEFAULT if registry is DEFAULT_REGISTRY else ToolCallRepairer(registry)
    return engine.repair(text, multi=multi)


def parse(text: str, registry: ToolRegistry = DEFAULT_REGISTRY) -> Optional[ToolCall]:
    """Repair and return the first :class:`ToolCall` in *text*."""
    return (_DEFAULT if registry is DEFAULT_REGISTRY else ToolCallRepairer(registry)).parse(text)


def parse_all(text: str, registry: ToolRegistry = DEFAULT_REGISTRY) -> List[ToolCall]:
    """Repair and return every :class:`ToolCall` in *text*."""
    return (_DEFAULT if registry is DEFAULT_REGISTRY else ToolCallRepairer(registry)).parse_all(text)


find_tool_calls = parse_all
