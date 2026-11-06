"""Python structure: functions (qualname, length, nesting, body hash) and scope lookup.

Cyclomatic complexity comes from ruff (``ruff_runner``) so the number matches what
``ruff check --select C901`` prints; everything else is measured here from the AST.
"""

from __future__ import annotations

import ast
import hashlib
from bisect import bisect_right
from dataclasses import dataclass

from scripts.code_health.model import MODULE_SCOPE, FileMeasure, Unit

_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
_BLOCKS = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With, ast.AsyncWith, ast.Match)
_TRY_STAR = getattr(ast, "TryStar", None)
if _TRY_STAR is not None:
    _BLOCKS = (*_BLOCKS, _TRY_STAR)


@dataclass
class Span:
    start: int
    end: int
    qualname: str


def _child_blocks(node: ast.AST):
    """Statement lists directly owned by a compound statement."""
    for name in ("body", "orelse", "finalbody"):
        yield getattr(node, name, None) or []
    for handler in getattr(node, "handlers", None) or []:
        yield handler.body
    for case in getattr(node, "cases", None) or []:
        yield case.body


def nesting_depth(stmts: list[ast.stmt], depth: int = 0) -> int:
    deepest = depth
    for stmt in stmts:
        if isinstance(stmt, (*_FUNCS, ast.ClassDef)) or not isinstance(stmt, _BLOCKS):
            continue
        for index, block in enumerate(_child_blocks(stmt)):
            # `elif` is an If alone in orelse that starts at its `if`'s column (the `elif`
            # keyword); `else:` + an indented `if` has the same AST but is nested.
            is_elif = (
                isinstance(stmt, ast.If)
                and index == 1
                and len(block) == 1
                and isinstance(block[0], ast.If)
                and block[0].col_offset == stmt.col_offset
            )
            deepest = max(deepest, nesting_depth(block, depth if is_elif else depth + 1))
    return deepest


_SCOPES = (*_FUNCS, ast.Lambda, ast.ClassDef, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def _own_scope(node: ast.AST):
    """Nodes in ``node``'s own scope: a nested def/lambda/class/comprehension is yielded but not
    entered (its parameters and targets bind inside it, not here)."""
    stack = list(ast.iter_child_nodes(node))
    while stack:
        child = stack.pop()
        yield child
        if not isinstance(child, _SCOPES):
            stack.extend(ast.iter_child_nodes(child))


def _bound_names(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.Name):
        return () if isinstance(node.ctx, ast.Load) else (node.id,)
    if isinstance(node, ast.arg):
        return (node.arg,)
    if isinstance(node, ast.alias):
        return ((node.asname or node.name).split(".")[0],)
    if isinstance(node, (ast.Global, ast.Nonlocal)):
        return tuple(node.names)
    # def/class names, `except ... as name`, match captures and `**rest`
    found = (getattr(node, "name", None), getattr(node, "rest", None))
    return tuple(n for n in found if isinstance(n, str))


def _binds(scope: ast.AST, name: str) -> bool:
    return any(name in _bound_names(child) for child in _own_scope(scope))


def _bindings(scope: ast.AST, name: str) -> list[ast.AST]:
    return [child for child in _own_scope(scope) if name in _bound_names(child)]


def _lexical_binding(tree: ast.Module, node: ast.AST, name: str) -> ast.AST | None:
    """Unique lexical value, with straight-line local imports allowed to replace args.

    Conditional, repeated, wildcard and closure writes are unknown. Class namespaces
    are not lexical parents of method bodies; surrounding function namespaces are.
    """
    local = _bindings(node, name)
    if local:
        aliases = [binding for binding in local if isinstance(binding, ast.alias)]
        if len(aliases) != 1 or any(binding is not aliases[0] and not isinstance(binding, ast.arg)
                                    for binding in local):
            return None
        body = getattr(node, 'body', [])
        # Only a leading docstring is non-executable; later barriers still stop proof.
        if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body = body[1:]
        for statement in body:
            if not isinstance(statement, (ast.Import, ast.ImportFrom)):
                break
            if aliases[0] in statement.names and all(a.name != '*' for a in statement.names):
                return aliases[0]
        return None

    def ancestors(parent, chain=()):
        if type(parent) is type(node) and getattr(parent, 'lineno', None) == getattr(node, 'lineno', None):
            return chain
        for child in ast.iter_child_nodes(parent):
            found = ancestors(child, (*chain, parent))
            if found is not None:
                return found
        return None
    chain = ancestors(tree)
    if chain is None or any(_binds(parent, name) for parent in chain if isinstance(parent, (*_FUNCS, ast.Lambda))):
        return None
    if any(isinstance(n, ast.ImportFrom) and any(a.name == '*' for a in n.names) for n in _own_scope(tree)):
        return None
    bindings = _bindings(tree, name)
    if len(bindings) != 1:
        return None
    binding = bindings[0]
    # A conditional import is a binding, but not proof that the value exists.
    return binding if any(binding is statement or binding in getattr(statement, 'names', [])
                          for statement in tree.body) else None


def _imported_callable(tree: ast.Module, node: ast.AST, name: str) -> tuple[str, str] | None:
    binding = _lexical_binding(tree, node, name)
    if not isinstance(binding, ast.alias):
        return None
    for statement in [*tree.body, *getattr(node, 'body', [])]:
        if (isinstance(statement, ast.ImportFrom) and not statement.level and statement.module
                and binding in statement.names):
            return statement.module, binding.name
    return None


def _module_alias(tree: ast.Module, node: ast.AST, module: str, name: str) -> bool:
    binding = _lexical_binding(tree, node, name)
    if not isinstance(binding, ast.alias):
        return False
    return any(isinstance(statement, ast.Import) and binding in statement.names and binding.name == module
               for statement in [*tree.body, *getattr(node, 'body', [])])


def execution_kind(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    """A call enters a plain function, but only await/scheduling enters a coroutine.

    Generators and decorators need a separate consumption/identity proof. Nested
    scopes' yields do not defer the containing function's body.
    """
    if node.decorator_list or any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in _own_scope(node)):
        return None
    return 'async' if isinstance(node, ast.AsyncFunctionDef) else 'sync'


def _self_references(node: ast.AST, name: str) -> list[ast.Name | ast.Attribute]:
    """References in ``node`` to its own ``name``: ``name`` loads that no nested scope rebinds,
    and ``self.name`` / ``cls.name``."""
    refs: list[ast.Name | ast.Attribute] = []
    todo = [node]
    while todo:
        for child in _own_scope(todo.pop()):
            if isinstance(child, _SCOPES):
                if not _binds(child, name):
                    todo.append(child)
            elif isinstance(child, ast.Name) and child.id == name and isinstance(child.ctx, ast.Load) or (isinstance(child, ast.Attribute) and child.attr == name
                  and isinstance(child.ctx, ast.Load)
                  and isinstance(child.value, ast.Name) and child.value.id in ("self", "cls")):
                refs.append(child)
    return refs


def body_hash(node: ast.AST) -> str:
    """Name-independent hash, so a function moved or renamed unchanged keeps its cap.

    The declared name is dropped, and so are the body's references to it (``name(...)``,
    ``self.name(...)``, ``cls.name(...)``), unless the function's own scope rebinds that name:
    a recursive function renamed together with its self-call is still the same code. A nested
    scope that binds the name (``def identity(legacy)``) only keeps its own reads.
    """
    name = getattr(node, "name", "")
    refs = _self_references(node, name) if name and not _binds(node, name) else []
    # Blank the references in place for the dump, then put them back: the tree is shared.
    saved = [(ref, ref.id if isinstance(ref, ast.Name) else ref.attr) for ref in refs]
    for ref, _ in saved:
        _rename(ref, "")
    try:
        dumped = ast.dump(node, annotate_fields=False).replace(repr(name), "''", 1)
    finally:
        for ref, original in saved:
            _rename(ref, original)
    return hashlib.sha1(dumped.encode("utf-8")).hexdigest()[:16]


def _rename(ref: ast.Name | ast.Attribute, value: str) -> None:
    if isinstance(ref, ast.Name):
        ref.id = value
    else:
        ref.attr = value


def _first_line(node: ast.AST) -> int:
    """A def/class starts at its first decorator: decorator code belongs to it (and moves
    with it), not to the module."""
    return min([node.lineno, *(d.lineno for d in getattr(node, "decorator_list", []))])


class _UnitCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.stack: list[str] = []
        self.units: dict[str, Unit] = {}
        self.spans: list[Span] = []
        self.seen: set[str] = set()
        self.funcs: list[str] = []
        self.total_lines: dict[str, int] = {}

    def _qualname(self, name: str) -> str:
        """Dotted path without ``<locals>``; a repeated name (property setter, conditional
        def) gets ``#2``, ``#3`` in source order."""
        base = ".".join([*self.stack, name])
        qual, n = base, 1
        while qual in self.seen:
            n += 1
            qual = f"{base}#{n}"
        self.seen.add(qual)
        return qual

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qual = self._qualname(node.name)
        # A class has no size metrics of its own, but it is a unit so that a renamed or moved
        # class keeps the violations in its body (decorators, attributes) one-to-one.
        self.units[qual] = Unit(qualname=qual, line=node.lineno, metrics={},
                                body_hash=body_hash(node), parent=self.funcs[-1] if self.funcs else None,
                                end_line=node.end_lineno)
        self.spans.append(Span(_first_line(node), node.end_lineno or node.lineno, qual))
        self.stack.append(qual.rsplit(".", 1)[-1])
        self.generic_visit(node)
        self.stack.pop()

    def _visit_func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        qual = self._qualname(node.name)
        end = node.end_lineno or node.lineno
        first = _first_line(node)
        self.total_lines[qual] = end - first + 1
        self.units[qual] = Unit(
            qualname=qual,
            line=node.lineno,
            metrics={"FUNC_LINES": end - first + 1, "NESTING": nesting_depth(node.body)},
            body_hash=body_hash(node),
            parent=self.funcs[-1] if self.funcs else None,
            end_line=end,
        )
        self.spans.append(Span(first, end, qual))
        self.stack.append(qual.rsplit(".", 1)[-1])
        self.funcs.append(qual)
        self.generic_visit(node)
        self.funcs.pop()
        self.stack.pop()

    visit_FunctionDef = _visit_func
    visit_AsyncFunctionDef = _visit_func


class ScopeIndex:
    """Innermost enclosing def/class qualname for a line."""

    def __init__(self, spans: list[Span]) -> None:
        self.spans = sorted(spans, key=lambda s: (s.start, -s.end))
        self.starts = [s.start for s in self.spans]

    def scope(self, line: int) -> str:
        best = MODULE_SCOPE
        best_size = None
        for span in self.spans[: bisect_right(self.starts, line)]:
            if span.start <= line <= span.end:
                size = span.end - span.start
                if best_size is None or size <= best_size:
                    best, best_size = span.qualname, size
        return best


def measure_structure(fm: FileMeasure, tree: ast.Module) -> ScopeIndex:
    collector = _UnitCollector()
    collector.visit(tree)
    # FUNC_LINES is a function's OWN lines: a nested def is its own unit, so editing a closure
    # never counts against the function that encloses it.
    for unit in collector.units.values():
        if unit.parent is not None and unit.qualname in collector.total_lines:
            parent = collector.units[unit.parent]
            parent.metrics["FUNC_LINES"] -= collector.total_lines[unit.qualname]
    fm.units = collector.units
    fm.metrics["FILE_LINES"] = len(fm.lines)
    return ScopeIndex(collector.spans)


def extraction_functions(fm: FileMeasure) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """AST functions keyed by the measurer's scope, never inferred from hit text."""
    if not fm.path.endswith(".py") or fm.error:
        return {}
    try:
        tree = ast.parse("\n".join(fm.lines))
    except SyntaxError:
        return {}
    by_line = {u.line: q for q, u in fm.units.items()}
    return {by_line[n.lineno]: n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.lineno in by_line}


def _return_call(node: ast.FunctionDef | ast.AsyncFunctionDef) -> ast.Call | None:
    body = [s for s in node.body if not isinstance(s, (ast.Import, ast.ImportFrom))
            and not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)
                     and isinstance(s.value.value, str))]
    if len(body) != 1 or not isinstance(body[0], ast.Return):
        return None
    value = body[0].value
    return value.value if isinstance(value, ast.Await) and isinstance(value.value, ast.Call) else (
        value if isinstance(value, ast.Call) else None)


def _bound_callback(consumer, call, helper, *, bound):
    """Bind a supplied method reference to one unambiguous formal, not an AST descendant."""
    parameters = [a.arg for a in (*consumer.args.posonlyargs, *consumer.args.args)]
    if bound:
        parameters = parameters[1:]
    for index, arg in enumerate(call.args):
        if isinstance(arg, ast.Starred):
            return None
        if arg is helper:
            return parameters[index] if index < len(parameters) else None
    return next((kw.arg for kw in call.keywords if kw.value is helper
                 and kw.arg in parameters + [a.arg for a in consumer.args.kwonlyargs]), None)


def _callback_invoked(consumer, parameter, *, asynchronous, module):
    """Small, fail-closed consumer proof: reachable terminal invocation, no rebinding/defer.

    Admission guards may return early. Loops, try/finally and arbitrary callback routing
    are not proofs. Async callbacks must be awaited or scheduled by the Context.run /
    asyncio.create_task idiom; creating or storing a coroutine is not execution.
    """
    body = [s for s in consumer.body if not (isinstance(s, ast.Expr)
            and isinstance(s.value, ast.Constant) and isinstance(s.value.value, str))]
    if not body or not isinstance(body[-1], ast.Return):
        return False
    own = list(_own_scope(consumer))
    if (consumer.decorator_list or any(isinstance(n, ast.Name) and n.id == parameter
            for statement in body[:-1] for n in _own_scope(statement))):
        return False
    if any(isinstance(n, (ast.Yield, ast.YieldFrom, ast.Global, ast.Nonlocal))
           or isinstance(n, ast.Name) and n.id == parameter and isinstance(n.ctx, (ast.Store, ast.Del))
           or isinstance(n, ast.alias) and (n.asname or n.name.split('.')[0]) == parameter
           for n in own):
        return False
    for statement in body[:-1]:
        if isinstance(statement, ast.If):
            if statement.orelse or isinstance(statement.test, ast.Constant):
                return False
        elif not isinstance(statement, (ast.Assign, ast.AnnAssign, ast.Import, ast.ImportFrom, ast.Expr)):
            return False
    value = body[-1].value
    awaited = isinstance(value, ast.Await)
    if awaited:
        value = value.value
    if not isinstance(value, ast.Call):
        return False
    if isinstance(value.func, ast.Name) and value.func.id == parameter:
        return not asynchronous or (awaited and isinstance(consumer, ast.AsyncFunctionDef))
    def module_call(n, module_name, attr):
        return (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and isinstance(n.func.value, ast.Name) and n.func.attr == attr
                and _module_alias(module, consumer, module_name, n.func.value.id))
    if (awaited and isinstance(consumer, ast.AsyncFunctionDef) and isinstance(value.func, ast.Attribute)
            and value.func.attr == 'run' and isinstance(value.func.value, ast.Call)
            and isinstance(value.func.value.func, ast.Attribute) and value.func.value.func.attr == 'copy'
            and isinstance(value.func.value.func.value, ast.Name) and len(value.args) == 2):
        context_name = value.func.value.func.value.id
        captures = [n.value for n in body[:-1] if isinstance(n, ast.Assign) and len(n.targets) == 1
                    and isinstance(n.targets[0], ast.Name) and n.targets[0].id == context_name]
        captured = captures[0] if len(captures) == 1 else None
        context_source = captured.body if isinstance(captured, ast.IfExp) else captured
        task, callback = value.args
        return (module_call(context_source, 'contextvars', 'copy_context')
                and isinstance(task, ast.Attribute) and isinstance(task.value, ast.Name)
                and task.attr == 'create_task' and _module_alias(module, consumer, 'asyncio', task.value.id)
                and isinstance(callback, ast.Call)
                and isinstance(callback.func, ast.Name) and callback.func.id == parameter)
    return False


def forwarded_helpers(node: ast.FunctionDef | ast.AsyncFunctionDef, scope: str,
                      functions: dict, sources: dict[str, FileMeasure], path: str) -> set[str]:
    """Credit a direct call or a resolved consumer's invoked callback, never mere references.

    Imported consumers must be supplied as measured source. Missing or ambiguous source is
    ordinary new debt; the checker does not import code, read live files or trust names.
    """
    call = _return_call(node)
    parent = scope.rpartition('.')[0]
    if call is None or not parent or execution_kind(node) is None:
        return set()
    def method(n):
        return (f'{parent}.{n.attr}' if isinstance(n, ast.Attribute)
                and isinstance(n.value, ast.Name) and n.value.id == 'self' else None)
    direct = method(call.func)
    consumer_module = ast.parse('\n'.join(sources[path].lines))
    def executes(function):
        kind = execution_kind(function)
        terminal = node.body[-1]
        return kind == 'sync' or (kind == 'async' and isinstance(terminal, ast.Return)
                                 and isinstance(terminal.value, ast.Await))
    if direct in functions:
        if sum(q.split('#')[0] == direct for q in functions) != 1 or not executes(functions[direct]):
            return set()
        consumer = functions[direct]
        bound = True
    else:
        # Resolve the call's lexical binding before inspecting supplied consumer source.
        tree = consumer_module
        binding = _imported_callable(tree, node, call.func.id) if isinstance(call.func, ast.Name) else None
        if binding is None:
            return set()
        module, name = binding
        fm = sources.get((module or '').replace('.', '/') + '.py')
        if fm is None:
            return set()
        candidates = extraction_functions(fm)
        named = [n for q, n in candidates.items() if q.split('#')[0] == name]
        if len(named) != 1 or not executes(named[0]):
            return set()
        consumer = named[0]
        consumer_module = ast.parse('\n'.join(fm.lines))
        bindings = _bindings(consumer_module, name)
        if (len(bindings) != 1 or not isinstance(bindings[0], _FUNCS)
                or bindings[0].lineno != consumer.lineno
                or any(isinstance(n, ast.ImportFrom) and any(a.name == '*' for a in n.names)
                       for n in _own_scope(consumer_module))):
            return set()
        bound = False
    result = {direct} if direct else set()
    for helper in [*call.args, *(kw.value for kw in call.keywords)]:
        qual = method(helper)
        if (qual is None or qual not in functions
                or sum(q.split('#')[0] == qual for q in functions) != 1):
            continue
        kind = execution_kind(functions[qual])
        if kind is None:
            continue
        parameter = _bound_callback(consumer, call, helper, bound=bound)
        if parameter and _callback_invoked(consumer, parameter,
                asynchronous=kind == 'async', module=consumer_module):
            result.add(qual)
    return result


def exception_blocks(node: ast.AST) -> dict[str, list[ast.Try]]:
    """Whole try/handlers must survive, not just an identical except line."""
    blocks: dict[str, list[ast.Try]] = {}
    for child in _own_scope(node):
        if isinstance(child, ast.Try) and child.handlers:
            blocks.setdefault(ast.dump(child), []).append(child)
    return blocks


def extracted_body_survives(origin: ast.FunctionDef | ast.AsyncFunctionDef,
                            helper: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Ordered whole statements anchor a moved catch to its original surrounding operation."""
    def statements(node):
        return [ast.dump(s) for s in node.body if not (isinstance(s, ast.Expr)
                and isinstance(s.value, ast.Constant) and isinstance(s.value.value, str))]
    remaining = iter(statements(helper))
    return all(any(candidate == statement for candidate in remaining) for statement in statements(origin))
