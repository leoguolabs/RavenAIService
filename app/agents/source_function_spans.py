"""Optional C syntax ranges for bounded source excerpts, never runtime evidence."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
from pathlib import Path

MAX_PARSE_BYTES = 2 * 1024 * 1024


def source_identity(path: Path) -> tuple:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _read_source_bytes(path: Path) -> bytes:
    with path.open("rb") as stream:
        return stream.read(MAX_PARSE_BYTES + 1)


def source_snapshot(path: Path) -> tuple[tuple, bytes]:
    """Bounded content validation also handles coarse filesystem timestamps."""
    identity = source_identity(path)
    if identity[2] > MAX_PARSE_BYTES:
        raise ValueError("source exceeds context limit")
    raw = _read_source_bytes(path)
    if (len(raw) > MAX_PARSE_BYTES or source_identity(path) != identity
            or _read_source_bytes(path) != raw or source_identity(path) != identity):
        raise ValueError("source changed during context read")
    return (*identity, hashlib.sha256(raw).digest()), raw


@dataclass(frozen=True)
class FunctionSpan:
    name: str
    start_line: int
    end_line: int
    declaration_end_line: int


class CFunctionSpans:
    """Per-run, content-validated ranges; missing parsers retain native windows.

    No source text or syntax tree survives parsing. A syntactically complete
    function does not establish a compiled branch or the deployed revision.
    Callers must validate workspace containment before passing a path here.
    """

    def __init__(self):
        self.cache: OrderedDict[Path, tuple[tuple, tuple[FunctionSpan, ...]]] = OrderedDict()
        self._parser = None
        self._unavailable = False

    def _parse(self, raw: bytes) -> tuple[FunctionSpan, ...]:
        if self._unavailable:
            return ()
        if self._parser is None:
            try:
                from tree_sitter import Language, Parser
                import tree_sitter_c

                self._parser = Parser(Language(tree_sitter_c.language()))
            except (ImportError, TypeError, ValueError):
                self._unavailable = True
                return ()
        tree = self._parser.parse(raw)
        if tree is None:
            return ()
        spans = []
        pending = [tree.root_node]
        while pending:
            node = pending.pop()
            if node.type != "function_definition":
                pending.extend(reversed(node.named_children))
                continue
            if node.has_error:
                continue
            body = node.child_by_field_name("body")
            name = node.child_by_field_name("declarator")
            while name is not None and name.type != "identifier":
                name = name.child_by_field_name("declarator")
            if body is None or body.type != "compound_statement" or name is None:
                continue
            spans.append(FunctionSpan(
                raw[name.start_byte:name.end_byte].decode("utf-8"),
                node.start_point.row + 1, node.end_point.row + 1,
                body.start_point.row + 1,
            ))
        return tuple(spans)

    def get(self, path: Path, snapshot: tuple[tuple, bytes] | None = None) -> tuple[FunctionSpan, ...]:
        if path.suffix.lower() not in (".c", ".h"):
            return ()
        try:
            identity, raw = source_snapshot(path) if snapshot is None else snapshot
        except (OSError, ValueError):
            return ()
        cached = self.cache.get(path)
        if cached is not None and cached[0] == identity:
            self.cache.move_to_end(path)
            return cached[1]
        if len(raw) > MAX_PARSE_BYTES or b"\0" in raw:
            return ()
        try:
            raw.decode("utf-8")
            spans = self._parse(raw)
        except (UnicodeError, TypeError, ValueError):
            return ()
        if source_identity(path) != identity[:-1]:
            return ()
        self.cache[path] = identity, spans
        self.cache.move_to_end(path)
        while len(self.cache) > 4:
            self.cache.popitem(last=False)
        return spans

    def containing(self, path: Path, start: int, end: int | None = None,
                   snapshot: tuple[tuple, bytes] | None = None) -> FunctionSpan | None:
        end = start if end is None else end
        return next((span for span in self.get(path, snapshot)
                     if span.start_line <= start <= end <= span.end_line), None)
