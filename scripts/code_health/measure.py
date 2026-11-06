"""Measure a set of files in one tree (a revision or the working tree)."""

from __future__ import annotations

import ast
import io
import tokenize
import json
import re
import tempfile
import warnings
from pathlib import Path

from scripts.code_health import py_rules, py_structure
from scripts.code_health.config import RULES, RULES_BY_ID, in_scope, rule_applies
from scripts.code_health.gitio import read_file
from scripts.code_health.model import FileMeasure, Unit
from scripts.code_health.ruff_runner import run_ruff
from scripts.code_health.ts_measure import measure_ts

_PATTERNS_FILE = Path("scripts/ci/profile_scope_patterns.json")


def _regex_rules(repo: Path) -> list[tuple[str, re.Pattern[str], re.Pattern[str] | None]]:
    data = json.loads((repo / _PATTERNS_FILE).read_text(encoding="utf-8-sig"))
    by_id = {p["id"]: p for p in data["patterns"]}
    out = []
    for rule in RULES:
        if rule.source != "regex":
            continue
        pat = by_id[rule.pattern_id]
        path_re = re.compile(pat["path_regex"]) if pat.get("path_regex") else None
        out.append((rule.id, re.compile(pat["pattern_regex"]), path_re))
    return out


def _own_complexity(fm: FileMeasure, cc_by_line: dict[int, int]) -> None:
    """Ruff's C901 for a function includes every nested def; subtract the direct children so
    each function carries only its own branches (nested defs are separate units)."""
    full = {q: cc_by_line[u.line] for q, u in fm.units.items() if u.line in cc_by_line}
    own = dict(full)
    for qual, unit in fm.units.items():
        if unit.parent in own and qual in full:
            own[unit.parent] -= full[qual]
    for qual, value in own.items():
        fm.units[qual].metrics["CC"] = value


def _python_comments(text: str) -> dict[int, str]:
    comments: dict[int, str] = {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                comments[tok.start[0]] = comments.get(tok.start[0], "") + tok.string
    except (tokenize.TokenError, SyntaxError):
        pass  # unparseable source has no AST findings to waive either
    return comments


# Characters str.splitlines() breaks on; never blanked, so the blanked text keeps its line numbers.
_LINE_BREAKS = frozenset("\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029")


def _string_statements(tree: ast.Module):
    """Docstrings and other bare string statements: prose, never executed."""
    for node in ast.walk(tree):
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            yield node


def _executable_lines(text: str, tree: ast.Module) -> list[str]:
    """``text.splitlines()`` with comments and string statements blanked out.

    The profile regex rules describe operations (`env = os.environ.copy()`), so a comment or a
    docstring that mentions one must not count as doing it. String literals inside code stay:
    `os.getenv("DISCORD_TOKEN")` is matched by its argument.
    """
    rows = [list(line) for line in io.StringIO(text).readlines()]  # ast/tokenize line numbering

    def blank(line: int, start: int, end: int | None = None) -> None:
        row = rows[line - 1]
        for col in range(start, len(row) if end is None else end):
            if row[col] not in _LINE_BREAKS:
                row[col] = " "

    def char_col(line: int, byte_col: int) -> int:  # ast columns are UTF-8 byte offsets
        return len("".join(rows[line - 1]).encode("utf-8")[:byte_col].decode("utf-8", "ignore"))

    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                blank(tok.start[0], tok.start[1], tok.end[1])
    except (tokenize.TokenError, SyntaxError):
        return text.splitlines()  # fail closed: every line stays visible to the rules
    for node in _string_statements(tree):
        last = node.end_lineno or node.lineno
        end = char_col(last, node.end_col_offset or 0)
        if last == node.lineno:
            blank(node.lineno, char_col(node.lineno, node.col_offset), end)
            continue
        blank(node.lineno, char_col(node.lineno, node.col_offset))
        for line in range(node.lineno + 1, last):
            blank(line, 0)
        blank(last, 0, end)
    return "".join("".join(row) for row in rows).splitlines()


def _body(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.stmt]:
    """Executable statements, excluding a leading docstring."""
    return [s for s in node.body if not (isinstance(s, ast.Expr)
            and isinstance(s.value, ast.Constant) and isinstance(s.value.value, str))]


def _self_attr(node: ast.AST | None) -> str | None:
    return node.attr if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                         and node.value.id == "self") else None


def _publisher(method: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str, str] | None:
    """Exact object assignment followed by the paired readiness signal, not a name convention."""
    body = _body(method)
    args = method.args.args
    if (len(args) != 2 or len(body) != 2 or method.decorator_list
            or not isinstance(method, ast.FunctionDef)):
        return None
    if (args[0].arg != "self" or method.args.defaults or method.args.posonlyargs
            or method.args.kwonlyargs or method.args.vararg or method.args.kwarg):
        return None
    assign, signal = body
    if not (isinstance(assign, ast.Assign) and len(assign.targets) == 1
            and isinstance(assign.value, ast.Name) and assign.value.id == args[1].arg
            and isinstance(signal, ast.Expr) and isinstance(signal.value, ast.Call)):
        return None
    field = _self_attr(assign.targets[0])
    call = signal.value
    if not (isinstance(call.func, ast.Attribute) and call.func.attr == "set"
            and not call.args and not call.keywords):
        return None
    event = _self_attr(call.func.value)
    return (field, event) if field and event and field != event else None


def _waits_and_raises(wait: ast.AST, check: ast.AST, field: str, event: str) -> bool:
    if not (isinstance(wait, ast.Try) and len(wait.body) == 1 and not wait.orelse and not wait.finalbody
            and all(h.body and isinstance(h.body[-1], ast.Raise) and h.body[-1].exc is None
                    for h in wait.handlers)):
        return False
    statement = wait.body[0]
    if not (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Await)
            and isinstance(statement.value.value, ast.Call)):
        return False
    call = statement.value.value
    if not (isinstance(call.func, ast.Attribute) and call.func.attr == "wait"
            and _self_attr(call.func.value) == event and not call.args and not call.keywords):
        return False
    return (isinstance(check, ast.If) and _self_attr(check.test) == field and not check.orelse
            and len(check.body) == 1 and isinstance(check.body[0], ast.Raise)
            and _self_attr(check.body[0].exc) == field and check.body[0].cause is None)


def _coroutine(method: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return py_structure.execution_kind(method) == 'async'


def _starts_owner(method: ast.FunctionDef | ast.AsyncFunctionDef, producer: str, field: str, event: str) -> bool:
    if not _coroutine(method):
        return False
    if any(isinstance(n, ast.Name) and n.id == "asyncio" and isinstance(n.ctx, (ast.Store, ast.Del))
           or isinstance(n, ast.arg) and n.arg == "asyncio"
           for n in py_structure._own_scope(method)):
        return False
    body = _body(method)
    for index, statement in enumerate(body[:-2]):
        # Only a straight-line setup can establish the owner's reachable suffix.
        # Nested defs/lambdas cannot make a return/yield in this scope disappear.
        if any(not isinstance(s, (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign, ast.Expr))
               or any(isinstance(n, (ast.Return, ast.Raise, ast.Yield, ast.YieldFrom, ast.NamedExpr))
                      for n in py_structure._own_scope(s)) for s in body[:index]):
            continue
        if not (isinstance(statement, ast.Assign) and len(statement.targets) == 1
                and _self_attr(statement.targets[0]) and isinstance(statement.value, ast.Call)):
            continue
        call = statement.value
        if not (isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "asyncio" and call.func.attr == "create_task" and len(call.args) == 1):
            continue
        task = call.args[0]
        if isinstance(task, ast.Call) and _self_attr(task.func) == producer:
            if _waits_and_raises(body[index + 1], body[index + 2], field, event):
                return True
    return False


def _forwarded_pair(handler: ast.ExceptHandler, publishers: dict[str, tuple[str, str]]) -> tuple[str, str] | None:
    if not handler.name or len(handler.body) != 1 or not isinstance(handler.body[0], ast.Expr):
        return None
    call = handler.body[0].value
    if not (isinstance(call, ast.Call) and len(call.args) == 1 and not call.keywords
            and isinstance(call.args[0], ast.Name) and call.args[0].id == handler.name):
        return None
    return publishers.get(_self_attr(call.func) or "")


def _post_handler(producer: ast.AST, handler: ast.ExceptHandler) -> list[ast.stmt] | None:
    """Reachable continuation, or no proof when an enclosing loop can re-enter code.

    A branch's return ends fallthrough into its enclosing siblings, but cannot skip a
    finally. Loops also revisit earlier statements and may execute an else suite; a
    forward suffix alone cannot prove delivery there. Unrelated branches stay excluded.
    """
    parents = {}
    for parent in [producer, *py_structure._own_scope(producer)]:
        if parent is not producer and isinstance(parent, py_structure._SCOPES):
            continue
        for field, value in ast.iter_fields(parent):
            if isinstance(value, list):
                for index, child in enumerate(value):
                    if isinstance(child, ast.AST):
                        parents[id(child)] = (parent, field, index, value)
    statements, fallthrough = [], True
    child = handler
    while id(child) in parents:
        parent, field, index, siblings = parents[id(child)]
        if isinstance(parent, (ast.For, ast.AsyncFor, ast.While)):
            return None
        if field in ('body', 'orelse', 'finalbody') and fallthrough:
            for statement in siblings[index + 1:]:
                statements.append(statement)
                if isinstance(statement, (ast.Return, ast.Raise)):
                    fallthrough = False
                    break
        if isinstance(parent, ast.Try) and field != 'finalbody':
            statements.extend(parent.finalbody)
            if any(isinstance(s, (ast.Return, ast.Raise)) for s in parent.finalbody):
                fallthrough = False
        child = parent
    return statements


def _field_reset(statements: list[ast.stmt], field: str, event: str, methods: dict,
                 seen: frozenset[str] = frozenset()) -> bool:
    """Follow post-catch known mutators. Unknown/deferred/recursive effects fail closed."""
    nodes = [n for statement in statements
             for n in [statement, *py_structure._own_scope(statement)]]
    if any(isinstance(n, ast.Attribute) and isinstance(n.ctx, (ast.Store, ast.Del))
           and _self_attr(n) in (field, event) for n in nodes):
        return True
    for n in nodes:
        if not isinstance(n, ast.Call):
            continue
        if isinstance(n.func, ast.Attribute) and _self_attr(n.func.value) == event:
            if n.func.attr != 'set':
                return True
            continue
        name = _self_attr(n.func)
        if name is None:
            continue
        helper = methods.get(name)
        if (helper is None or name in seen or not isinstance(helper, ast.FunctionDef)
                or helper.decorator_list):
            return True
        if _field_reset(_body(helper), field, event, methods, seen | {name}):
            return True
    return False


def _class_deliveries(cls: ast.ClassDef) -> set[int]:
    methods = [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    unique = {m.name: m for m in methods if sum(x.name == m.name for x in methods) == 1}
    publishers = {name: pair for name, method in unique.items() if (pair := _publisher(method))}
    delivered = set()
    for producer in unique.values():
        if not _coroutine(producer):
            continue
        for handler in (n for n in py_structure._own_scope(producer) if isinstance(n, ast.ExceptHandler)):
            pair = _forwarded_pair(handler, publishers)
            continuation = _post_handler(producer, handler)
            if pair and continuation is not None and not _field_reset(continuation, *pair, unique):
                if any(_starts_owner(reader, producer.name, *pair) for reader in unique.values()):
                    delivered.add(handler.lineno)
    return delivered


def _delivered_handlers(tree: ast.Module) -> set[int]:
    """Prove a same-object field/event handoff to its actual awaiting, re-raising owner.

    Unknown/replacement publishers, unpaired signals and disconnected readers remain findings.
    No runtime names or source paths participate in this structural proof.
    """
    imported = any(isinstance(n, ast.Import) and any(a.name == "asyncio" and not a.asname for a in n.names)
                   for n in tree.body)
    if not imported or any(isinstance(n, ast.Name) and n.id == "asyncio"
                           and isinstance(n.ctx, (ast.Store, ast.Del))
                           for n in py_structure._own_scope(tree)):
        return set()
    return {line for cls in ast.walk(tree) if isinstance(cls, ast.ClassDef)
            for line in _class_deliveries(cls)}


class Measurer:
    def __init__(self, repo: Path, ruff: list[str], known_env: set[str]) -> None:
        self.repo = repo
        self.ruff = ruff
        self.ctx = py_rules.Ctx(known_env=known_env)
        self.regex_rules = _regex_rules(repo)

    def measure(self, tree: str | None, paths: list[str]) -> dict[str, FileMeasure]:
        contents = {}
        for path in paths:
            if in_scope(path):
                text = read_file(self.repo, tree, path)
                if text is not None:
                    contents[path] = text
        if tree is None:
            return self._measure_in(self.repo, contents)
        with tempfile.TemporaryDirectory(prefix="code-health-") as tmp:
            root = Path(tmp)
            for path, text in contents.items():
                dest = root / path
                dest.parent.mkdir(parents=True, exist_ok=True)
                # newline="": the blob's own line endings, so ruff's line numbers match the
                # AST's (Windows text mode would turn a CRLF blob into CR CR LF).
                dest.write_text(text, encoding="utf-8", newline="")
            return self._measure_in(root, contents)

    def _measure_in(self, root: Path, contents: dict[str, str]) -> dict[str, FileMeasure]:
        result = {path: FileMeasure(path, lines=text.splitlines()) for path, text in contents.items()}
        py = sorted(p for p in contents if in_scope(p) == "py")
        ts = sorted(p for p in contents if in_scope(p) == "ts")
        ruff_out = run_ruff(self.ruff, root, py) if py else {}
        for path in py:
            self._python(result[path], contents[path], ruff_out.get(path))
        ts_out = measure_ts(self.repo, root, ts) if ts else {}
        for path in ts:
            self._typescript(result[path], ts_out.get(path))
        return result

    def _python(self, fm: FileMeasure, text: str, ruff_file) -> None:
        fm.metrics["FILE_LINES"] = len(fm.lines)
        fm.comments = _python_comments(text)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                tree = ast.parse(text)
                rules_tree = py_rules.canonical_tree(ast.parse(text))
        except SyntaxError as exc:
            fm.error = f"does not parse: {exc.msg} (line {exc.lineno})"
            return
        scopes = py_structure.measure_structure(fm, tree)
        delivered = _delivered_handlers(tree)
        if ruff_file is None or ruff_file.errors:
            fm.error = "ruff could not measure it: " + "; ".join((ruff_file.errors if ruff_file else ["no result"])[:3])
        else:
            _own_complexity(fm, ruff_file.cc_by_line)
            for code, row in ruff_file.hits:
                if code == "BLE001" and row in delivered:
                    continue
                if code in RULES_BY_ID and rule_applies(RULES_BY_ID[code], fm.path):
                    fm.add_hit(code, scopes.scope(row), row)
        for rule_id, checker in py_rules.CHECKERS.items():
            if rule_applies(RULES_BY_ID[rule_id], fm.path):
                for row in sorted(set(checker(rules_tree, self.ctx))):
                    fm.add_hit(rule_id, scopes.scope(row), row)
        self._regex(fm, scopes, text, tree)

    def _regex(self, fm: FileMeasure, scopes, text: str, tree: ast.Module) -> None:
        code_lines: list[str] | None = None
        for rule_id, pattern, path_re in self.regex_rules:
            if not rule_applies(RULES_BY_ID[rule_id], fm.path):
                continue
            if path_re and not path_re.search(fm.path):
                continue
            if code_lines is None:
                code_lines = _executable_lines(text, tree)
            for index, line in enumerate(code_lines, start=1):
                if pattern.search(line):
                    fm.add_hit(rule_id, scopes.scope(index), index)

    @staticmethod
    def _typescript(fm: FileMeasure, data: dict | None) -> None:
        fm.metrics["FILE_LINES"] = len(fm.lines)
        if data is None or "error" in data:
            fm.error = (data or {}).get("error", "the TypeScript measurer returned nothing for it")
            return
        fm.comments = {line: text for line, text in data.get("comments", [])}
        for unit in data.get("units", []):
            fm.units[unit["q"]] = Unit(
                qualname=unit["q"],
                line=unit["line"],
                metrics={"CC": unit["cc"], "FUNC_LINES": unit["lines"], "NESTING": unit["nesting"]},
                body_hash=unit["hash"],
                end_line=unit.get("end"),
            )
