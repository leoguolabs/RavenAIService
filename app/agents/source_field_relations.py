"""Bounded current-source field relations from verified log records; no runtime diagnosis."""

from pathlib import Path
import hashlib
import json
import os
import re
import time
from app.agents.log_overview import _RECORD
from app.agents.source_function_spans import source_snapshot, source_identity

MAX_ENTRIES = 4096
MAX_HEADERS = 8
MAX_BYTES = 2 * 1024 * 1024
MAX_SECONDS = 2.0
MAX_FIELDS = 16
MAX_OUTPUT = 8000
MAX_FORMAT_BYTES = 4096
MAX_SPECIFIERS = 64
FIELD = re.compile("\\b([A-Za-z_]\\w{1,63})\\s*=\\s*(?:0x[0-9A-Fa-f]+|[-+]?\\d+)\\b")


class SourceFieldRelations:
    """Ephemeral optional C scanner; return source facts, never a decoded event.

    No AST or log buffer is retained by the calling Agent after this callback.
    Unsupported syntax, aliases or format bindings produce no extra evidence.
    """

    def __init__(self, root):
        from tree_sitter import Language, Parser
        import tree_sitter_c

        self.root = Path(root).resolve()
        self.repo = (self.root / "repo").resolve()
        self.repo.relative_to(self.root)
        self.parser = Parser(Language(tree_sitter_c.language()))
        self.cache = {}
        self.bytes = 0
        self.started = time.monotonic()
        self.files = []
        self.snapshots = {}
        self.logs = {}
        self.logbytes = 0

    def limit(self):
        if time.monotonic() - self.started > MAX_SECONDS:
            raise ValueError("time bound")

    def index(self):
        entries = 0
        for directory, dirs, names in os.walk(self.repo, followlinks=False):
            self.limit()
            entries += len(dirs) + len(names)
            if entries > MAX_ENTRIES:
                raise ValueError("incomplete source enumeration")
            dirs[:] = sorted(
                (
                    d
                    for d in dirs
                    if d != ".git" and (not (Path(directory) / d).is_symlink())
                )
            )
            for name in sorted(names):
                p = Path(directory) / name
                if (
                    p.suffix.lower() in (".c", ".h")
                    and (not p.is_symlink())
                    and p.is_file()
                ):
                    self.files.append(p)

    def read(self, path):
        path = path.resolve(strict=True)
        path.relative_to(self.repo)
        self.limit()
        if path not in self.cache:
            (identity, raw) = source_snapshot(path)
            if self.bytes + len(raw) > MAX_BYTES:
                raise ValueError("source byte bound")
            raw.decode("utf-8")
            self.bytes += len(raw)
            tree = self.parser.parse(raw)
            if tree is None:
                raise ValueError("source parser unavailable")
            self.cache[path] = (identity, raw, tree)
            self.snapshots[path] = identity
        return self.cache[path]

    def nodes(self, node):
        todo = [node]
        count = 0
        while todo:
            count += 1
            if count > 200000:
                raise ValueError("syntax node bound")
            if count % 256 == 0:
                self.limit()
            cur = todo.pop()
            yield cur
            todo.extend(reversed(cur.named_children))

    @staticmethod
    def name(node, raw):
        return raw[node.start_byte : node.end_byte].decode() if node is not None else ""

    def span(self, path, start, end):
        (identity, raw, _) = self.read(path)
        if end - start >= 160:
            raise ValueError("range bound")
        lines = raw.decode().splitlines()
        return {
            "path": str(path.relative_to(self.root)),
            "sha256": identity[-1].hex(),
            "lines": [[i, 0, lines[i - 1]] for i in range(start, end + 1)],
        }

    def header(self, source, include):
        candidate = source.parent / include
        if candidate.exists() or candidate.is_symlink():
            if not candidate.is_file() or candidate.is_symlink():
                return None
            try:
                candidate.resolve().relative_to(self.repo)
                return candidate.resolve()
            except ValueError:
                return None
        found = [
            p
            for p in self.files
            if p.suffix == ".h"
            and (
                str(p.relative_to(self.repo)) == include
                or str(p.relative_to(self.repo)).endswith("/" + include)
            )
        ]
        return found[0] if len(found) == 1 else None

    def definitions(self, path, fields, functions, messages):
        """Link supported format arguments and comparisons through declared types."""
        (_, raw, tree) = self.read(path)
        nodes = list(self.nodes(tree.root_node))
        includes = []
        for n in nodes:
            if n.type != "preproc_include":
                continue
            value = self.name(n.child_by_field_name("path"), raw)
            if not re.fullmatch('"[A-Za-z0-9_./+-]+"', value):
                continue
            hp = self.header(path, value[1:-1])
            if hp and hp not in includes:
                includes.append(hp)
        alias = {}
        structs = {}
        declarations = {}
        macros = set()
        for n in nodes:
            if n.type in ("preproc_def", "preproc_function_def"):
                macros.add(self.name(n.child_by_field_name("name"), raw))

        def declarators(node):
            return [
                child
                for (i, child) in enumerate(node.children)
                if node.field_name_for_child(i) == "declarator"
            ]

        def identity(node, source):
            depth = 0
            while node is not None and node.type not in (
                "identifier",
                "type_identifier",
                "field_identifier",
            ):
                if node.type == "pointer_declarator":
                    depth += 1
                elif node.type not in ("parenthesized_declarator",):
                    return None
                node = node.child_by_field_name("declarator")
            return (self.name(node, source), depth) if node is not None else None

        for hp in includes[:MAX_HEADERS]:
            (_, hr, ht) = self.read(hp)
            for n in self.nodes(ht.root_node):
                if n.type in ("preproc_def", "preproc_function_def"):
                    macros.add(self.name(n.child_by_field_name("name"), hr))
                if n.type == "type_definition" and (not n.has_error):
                    typ = n.child_by_field_name("type")
                    if typ is None:
                        continue
                    if (
                        typ.type == "struct_specifier"
                        and typ.child_by_field_name("body") is not None
                    ):
                        body = typ.child_by_field_name("body")
                        key = ("struct", str(hp), typ.start_byte)
                        members = set()
                        for decl in body.named_children:
                            if decl.type == "field_declaration":
                                for member in declarators(decl):
                                    value = identity(member, hr)
                                    if value:
                                        members.add(value[0])
                        structs[key] = members
                        base = key
                    elif typ.type == "type_identifier":
                        base = ("alias", self.name(typ, hr))
                    else:
                        continue
                    for decl in declarators(n):
                        value = identity(decl, hr)
                        if value:
                            alias.setdefault(value[0], []).append((base, value[1]))
                if n.type == "enum_specifier" and (not n.has_error):
                    body = n.child_by_field_name("body")
                    if body is not None:
                        names = {
                            self.name(x.child_by_field_name("name"), hr)
                            for x in self.nodes(body)
                            if x.type == "enumerator"
                        }
                        for symbol in names:
                            declarations.setdefault(symbol, []).append(
                                (hp, n.start_point.row + 1, n.end_point.row + 1)
                            )

        def resolve(typ, depth, seen=()):
            if typ in seen or len(seen) >= 8:
                return None
            candidates = alias.get(typ, [])
            if len(candidates) != 1:
                return None
            (base, extra) = candidates[0]
            if base[0] == "alias":
                return resolve(base[1], depth + extra, (*seen, typ))
            return (base, depth + extra)

        fn_info = []
        shadowed = {}
        for n in nodes:
            if n.type != "function_definition" or n.has_error:
                continue
            dec = n.child_by_field_name("declarator")
            name = dec
            while name is not None and name.type != "identifier":
                name = name.child_by_field_name("declarator")
            params = {}
            if dec is not None:
                for par in self.nodes(dec):
                    if par.type != "parameter_declaration":
                        continue
                    value = identity(par.child_by_field_name("declarator"), raw)
                    typ = par.child_by_field_name("type")
                    if value and typ is not None:
                        canonical = resolve(self.name(typ, raw), value[1])
                        if canonical:
                            params[value[0]] = canonical
            shadow = set()
            for item in self.nodes(n):
                if item.type in (
                    "parameter_declaration",
                    "declaration",
                    "type_definition",
                ):
                    for decl in declarators(item):
                        cur = decl
                        while cur is not None and cur.type not in (
                            "identifier",
                            "type_identifier",
                        ):
                            cur = cur.child_by_field_name("declarator")
                        if cur is not None:
                            shadow.add(self.name(cur, raw))
                elif item.type == "enumerator":
                    shadow.add(self.name(item.child_by_field_name("name"), raw))
            shadowed[n.start_byte] = shadow
            fn_info.append((self.name(name, raw), params, n))

        def typed_member(member, params):
            if member is None or member.type != "field_expression":
                return None
            field = self.name(member.child_by_field_name("field"), raw)
            owner = member.child_by_field_name("argument")
            if owner is None or owner.type != "identifier":
                return None
            value = params.get(self.name(owner, raw))
            if not value:
                return None
            (base, depth) = value
            operator = self.name(member.child_by_field_name("operator"), raw)
            if depth != (1 if operator == "->" else 0) or field not in structs.get(
                base, set()
            ):
                return None
            return (field, base)

        def body_matches(fmt, spec, body):
            # Unique bounded separators avoid regex/backtracking over source-derived patterns.
            literals = [""]
            kinds = []
            last = 0
            for item in spec:
                literals[-1] += fmt[last : item.start()]
                if item[0] == "%%":
                    literals[-1] += "%"
                else:
                    kinds.append(item[0][-1])
                    literals.append("")
                last = item.end()
            literals[-1] += fmt[last:]
            if not body.startswith(literals[0]):
                return False
            pos = len(literals[0])
            patterns = {
                "d": "[ ]*[-+]?[0-9]+[ ]*",
                "i": "[ ]*[-+]?[0-9]+[ ]*",
                "u": "[ ]*[0-9]+[ ]*",
                "o": "[ ]*[0-7]+[ ]*",
                "x": "[ ]*(?:0[xX])?[0-9A-Fa-f]+[ ]*",
                "X": "[ ]*(?:0[xX])?[0-9A-Fa-f]+[ ]*",
                "p": "[ ]*(?:0[xX][0-9A-Fa-f]+|\\(nil\\))[ ]*",
            }
            for i, kind in enumerate(kinds):
                delim = literals[i + 1]
                if not delim:
                    if i != len(kinds) - 1:
                        return False
                    ends = [len(body)]
                else:
                    ends = []
                    at = body.find(delim, pos)
                    while at >= 0:
                        ends.append(at)
                        at = body.find(delim, at + 1)
                valid = []
                for end in ends:
                    chunk = body[pos:end]
                    if kind == "s":
                        ok = "\n" not in chunk
                    elif kind == "c":
                        ok = len(chunk) == 1
                    else:
                        ok = (
                            kind in patterns
                            and re.fullmatch(patterns[kind], chunk) is not None
                        )
                    if ok:
                        valid.append(end)
                    if len(valid) > 1:
                        break
                if len(valid) != 1:
                    return False
                pos = valid[0] + len(delim)
            return pos == len(body)

        def format_bindings(node, params, field, observed):
            found = []
            for call in self.nodes(node):
                if call.type != "call_expression" or call.has_error:
                    continue
                al = call.child_by_field_name("arguments")
                if al is None:
                    continue
                args = al.named_children
                for j, arg in enumerate(args):
                    literals = (
                        [arg]
                        if arg.type == "string_literal"
                        else arg.named_children
                        if arg.type == "concatenated_string"
                        else []
                    )
                    if not literals or any(
                        (x.type != "string_literal" for x in literals)
                    ):
                        continue
                    texts = [self.name(x, raw) for x in literals]
                    if sum((len(x.encode()) for x in texts)) > MAX_FORMAT_BYTES:
                        continue
                    if any(
                        (
                            not x.startswith('"') or re.search("\\\\[0-7xuU]", x)
                            for x in texts
                        )
                    ):
                        continue
                    escapes = {
                        "a": "\x07",
                        "b": "\x08",
                        "f": "\x0c",
                        "n": "\n",
                        "r": "\r",
                        "t": "\t",
                        "v": "\x0b",
                        "\\": "\\",
                        '"': '"',
                        "'": "'",
                        "?": "?",
                    }

                    def decode(text):
                        text = text[1:-1].replace("\\\n", "")
                        if re.search("\\\\[^abfnrtv\\\\\\\"\\'?]", text):
                            raise ValueError("unsupported C string escape")
                        return re.sub("\\\\(.)", lambda m: escapes[m[1]], text)

                    try:
                        fmt = "".join((decode(x) for x in texts)).rstrip("\r\n")
                    except ValueError:
                        continue
                    spec = list(
                        re.finditer(
                            "%(?:[-+ #0]*[0-9]*(?:\\.[0-9]+)?(?:hh|ll|[hljztL])?[diuoxXfFeEgGaAcspn%])",
                            fmt,
                        )
                    )
                    if len(spec) > MAX_SPECIFIERS:
                        continue
                    if "%" in re.sub(
                        "%(?:[-+ #0]*[0-9]*(?:\\.[0-9]+)?(?:hh|ll|[hljztL])?[diuoxXfFeEgGaAcspn%])",
                        "",
                        fmt,
                    ):
                        continue
                    if not any(
                        (body_matches(fmt, spec, message) for message in observed)
                    ):
                        continue
                    pos = j + 1
                    for token in spec:
                        if token[0] == "%%":
                            continue
                        label = re.search(
                            "\\b" + re.escape(field) + "\\s*=\\s*(?:0[xX])?$",
                            fmt[: token.start()],
                        )
                        if pos >= len(args):
                            if label:
                                found.append((None, {}))
                            break
                        value = typed_member(args[pos], params)
                        if label:
                            if (
                                value
                                and value[0] == field
                                and (token[0][-1] in "diuoxX")
                            ):
                                found.append(
                                    (
                                        value[1],
                                        {
                                            "path": str(path.relative_to(self.root)),
                                            "start_line": call.start_point.row + 1,
                                            "end_line": call.end_point.row + 1,
                                            "argument": self.name(args[pos], raw),
                                        },
                                    )
                                )
                            else:
                                found.append((None, {}))
                        pos += 1
            return found

        field_types = {f: set() for f in fields}
        bindings = {f: [] for f in fields}
        for fn, params, node in fn_info:
            for field in fields:
                if fn not in functions[field]:
                    continue
                for typ, evidence in format_bindings(
                    node, params, field, messages.get((field, fn), [])
                ):
                    field_types[field].add(typ)
                    bindings[field].append(evidence)
        relations = {f: [] for f in fields}
        for fn, params, node in fn_info:
            for n in self.nodes(node):
                if (
                    n.type != "binary_expression"
                    or n.has_error
                    or self.name(n.child_by_field_name("operator"), raw)
                    not in ("==", "!=")
                ):
                    continue
                (left, right) = (
                    n.child_by_field_name("left"),
                    n.child_by_field_name("right"),
                )
                for member, symbol in [(left, right), (right, left)]:
                    value = typed_member(member, params)
                    if (
                        value
                        and symbol is not None
                        and (symbol.type == "identifier")
                        and (
                            self.name(symbol, raw)
                            not in shadowed[node.start_byte] | macros
                        )
                    ):
                        (field, base) = value
                        if field in fields and field_types[field] == {base}:
                            relations[field].append(
                                (
                                    self.name(symbol, raw),
                                    n.start_point.row + 1,
                                    n.end_point.row + 1,
                                    base,
                                    fn,
                                )
                            )
        out = []
        for field, uses in relations.items():
            candidates = {
                decl for (symbol, *_) in uses for decl in declarations.get(symbol, [])
            }
            if len(candidates) != 1:
                continue
            (hp, start, end) = next(iter(candidates))
            matching = [
                r for r in uses if (hp, start, end) in declarations.get(r[0], [])
            ]
            (symbol, a, z, base, comparison_fn) = matching[0]
            try:
                enum = self.span(hp, start, end)
                comparison = self.span(path, a, z)
            except ValueError:
                continue
            out.append(
                {
                    "field": field,
                    "compared_symbol": symbol,
                    "canonical_struct_source": {
                        "path": str(Path(base[1]).relative_to(self.root)),
                        "start_byte": base[2],
                        "sha256": self.read(Path(base[1]))[0][-1].hex(),
                    },
                    "format_binding": bindings[field],
                    "log_functions": sorted(functions[field]),
                    "comparison_function": comparison_fn,
                    "relation": "Current source binds the printed field label to this member and compares the same struct member with this enum constant, resolving declared pointer aliases. Compiled branches, binary/source identity and runtime event state are not established.",
                    "comparison": comparison,
                    "enum_body": enum,
                    "includes_examined": len(includes[:MAX_HEADERS]),
                    "includes_not_examined": max(0, len(includes) - MAX_HEADERS),
                }
            )
        return out

    def log_lines(self, relative):
        candidate = self.root / relative
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValueError("invalid log path")
        if any(
            (p.is_symlink() for p in [candidate, *candidate.parents] if p != self.root)
        ):
            raise ValueError("log symlink")
        lp = candidate.resolve(strict=True)
        lp.relative_to(self.root / "logs")
        if lp not in self.logs:
            identity = source_identity(lp)
            if (
                identity[2] > 16 * 1024 * 1024
                or len(self.logs) >= 8
                or self.logbytes + identity[2] > 32 * 1024 * 1024
            ):
                raise ValueError("log bound")
            with lp.open("rb") as stream:
                lograw = stream.read(16 * 1024 * 1024 + 1)
            if len(lograw) != identity[2] or source_identity(lp) != identity:
                raise ValueError("log mutation")
            self.logbytes += len(lograw)
            self.logs[lp] = (
                identity,
                hashlib.sha256(lograw).digest(),
                lograw.decode().splitlines(),
            )
        lines = self.logs[lp][2]
        return lines

    def collect_references(self, references):
        """Revalidate bounded line/hash references after a repository becomes ready."""
        matches = []
        for (relative, number), digest in references:
            self.limit()
            lines = self.log_lines(relative)
            if 1 <= number <= len(lines):
                text = lines[number - 1]
                if hashlib.sha256(text.encode()).hexdigest() == digest:
                    matches.append((relative, number, text))
        return self.collect(matches)

    def collect(self, matches):
        groups = {}
        verified = []
        for relative, number, text in matches:
            self.limit()
            lines = self.log_lines(relative)
            if not 1 <= number <= len(lines) or lines[number - 1] != text:
                continue
            rec = _RECORD.fullmatch(text)
            if not rec:
                continue
            (filename, function, body) = (rec[3], rec[4], rec[6])
            if Path(filename).name != filename or Path(filename).suffix.lower() not in (
                ".c",
                ".h",
            ):
                continue
            fields = {m[1] for m in FIELD.finditer(body)}
            if not fields:
                continue
            group = groups.setdefault(
                filename, {"fields": set(), "functions": {}, "messages": {}}
            )
            group["fields"].update(fields)
            for field in fields:
                group["functions"].setdefault(field, set()).add(function)
                group["messages"].setdefault((field, function), set()).add(body)
            verified.append({"path": relative, "line": number})
        if not groups:
            return {
                "source_relations": [],
                "limits": "No supported verified log fields.",
                "log_references": [],
            }
        self.index()
        out = []
        for filename, group in sorted(groups.items()):
            files = [p for p in self.files if p.name == filename]
            if len(files) != 1:
                continue
            out.extend(
                self.definitions(
                    files[0],
                    set(sorted(group["fields"])[:MAX_FIELDS]),
                    group["functions"],
                    group["messages"],
                )
            )
        for lp, (identity, digest, _) in self.logs.items():
            with lp.open("rb") as stream:
                raw = stream.read(16 * 1024 * 1024 + 1)
            if (
                source_identity(lp) != identity
                or len(raw) != identity[2]
                or hashlib.sha256(raw).digest() != digest
            ):
                raise ValueError("log mutation")
        for p, identity in self.snapshots.items():
            if source_snapshot(p)[0] != identity:
                raise ValueError("source mutation")
        payload = {
            "source_relations": out,
            "limits": "Only bounded selected log fields and includes are checked. This is a current-source relation, not an exhaustive field domain. Active preprocessing, captured binary/source identity and event state are unverified. No values evaluated.",
            "log_references": verified[:8],
        }
        original_count = len(out)
        while len(json.dumps(payload, ensure_ascii=False).encode()) > MAX_OUTPUT:
            if payload["source_relations"]:
                payload["source_relations"].pop()
            elif payload["log_references"]:
                payload["log_references"].pop()
            else:
                raise ValueError("metadata budget")
        payload["omitted_relations"] = original_count - len(payload["source_relations"])
        if len(json.dumps(payload, ensure_ascii=False).encode()) > MAX_OUTPUT:
            raise ValueError("metadata budget")
        return payload
