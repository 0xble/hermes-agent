"""Bounded literal operands for the delegated knowledge boundary, not a shell jail.

Only known destructive commands/calls invert containment to protect ancestors.
Ordinary references retain the narrower protected-root/descendant policy.

Literal cwd transitions (shell ``cd``, Python ``os.chdir``, a literal
``subprocess`` ``cwd=``) are collected from the same recursive scan, at every
nesting level, so a ``bash -c 'cd <home>; rm -rf memories'`` body widens the
bases its own relative operands resolve against. Runtime-computed targets
(``$X``, backticks, ``os.getcwd()``) stay outside this lexical contract.
"""
from __future__ import annotations

import ast
import os
import re
import shlex


def shell_tokens(command: str) -> list[str]:
    # Reuse the pure shell scanner's word-aware comment handling: shlex's
    # commenters setting alone incorrectly strips a literal hash within a path.
    from tools.approval_detection import _iter_top_level_shell_segments

    tokens = []
    for segment in _iter_top_level_shell_segments(command):
        lexer = shlex.shlex(segment, posix=True, punctuation_chars=";&|<>()")
        lexer.whitespace_split, lexer.commenters = True, ""
        tokens.extend(lexer)
        tokens.append(";")
    return tokens


def literal_paths(
    command: str, *, python_source: bool = False, _depth: int = 0,
) -> tuple[list[str], list[str], list[str]]:
    """Return literal references, destructive operands and cwd transitions.

    Nothing is evaluated. Transitions from nested bodies are returned alongside
    the outer ones: the caller unions every literal transition into its bases,
    so a nested ``cd`` gives the nested relative operands a base to resolve
    against instead of being invisible to the outer tokenization.
    """
    if _depth > 4:
        raise ValueError("nested literal source exceeds knowledge scan bound")
    if python_source:
        return _python_paths(command, _depth)
    references, destructive, transitions = [], [], []
    segment = []
    for token in [*shell_tokens(command), ";"]:
        if token and set(token) <= set(";&|()\n"):
            if segment:
                refs, targets, moves = _shell_paths(segment, _depth)
                references.extend(refs)
                destructive.extend(targets)
                transitions.extend(moves)
                segment = []
        else:
            segment.append(token)
    return references, destructive, transitions


def _cd_target(name: str, args: list[str]) -> str | None:
    """Literal ``cd``/``pushd`` target, or None when it is runtime state.

    ``cd -`` (OLDPWD), ``cd "$X"`` and a bare ``pushd`` (stack swap) cannot be
    known lexically and stay under the documented not-enforced contract. A bare
    ``cd`` is the login home, which this process knows, so it counts.
    """
    operands = list(args)
    while operands and operands[0].startswith("-") and operands[0] not in {"-", "--"}:
        operands.pop(0)  # -L / -P / -e style options
    if operands and operands[0] == "--":
        operands.pop(0)
    if not operands:
        return "~" if name == "cd" else None
    target = operands[0]
    if target == "-" or "$" in target or "`" in target:
        return None
    return target


def _shell_paths(tokens: list[str], depth: int) -> tuple[list[str], list[str], list[str]]:
    refs, targets, moves = list(tokens), [], []
    words = list(tokens)
    # Explicit non-evaluating wrappers only. Assignment expansion is not used.
    while words:
        name = os.path.basename(words[0])
        if "=" in words[0] and not words[0].startswith("/"):
            words.pop(0)
        elif name in {"command", "exec", "sudo", "env", "nohup"}:
            words.pop(0)
            while words and words[0].startswith("-"):
                option = words.pop(0)
                if option in {"-u", "-g", "-h", "-p", "--user", "--group", "--host"} and words:
                    words.pop(0)
                if option == "--":
                    break
        else:
            break
    if not words:
        return refs, targets, moves
    name, args = os.path.basename(words[0]), words[1:]
    # Flat scan, not command-position only: ``builtin cd`` or an unknown
    # wrapper must not hide a transition. Over-widening only adds bases, which
    # is the conservative direction for a boundary.
    for index, token in enumerate(tokens):
        if token in {"cd", "pushd"} and (move := _cd_target(token, tokens[index + 1:])) is not None:
            moves.append(move)
    if name in {"sh", "bash", "zsh", "dash", "ksh"} or re.fullmatch(r"python(?:\d+(?:\.\d+)*)?(?:\.exe)?", name):
        for index, option in enumerate(args[:-1]):
            has_command_flag = option == "-c" or (
                name in {"sh", "bash", "zsh", "dash", "ksh"}
                and option.startswith("-")
                and not option.startswith("--")
                and option[1:].isalpha()
                and "c" in option[1:]
            )
            if has_command_flag:
                more_refs, more_targets, more_moves = literal_paths(
                    args[index + 1], python_source=name.startswith("python"), _depth=depth + 1)
                refs.extend(more_refs)
                targets.extend(more_targets)
                moves.extend(more_moves)
                break
    if name not in {"rm", "rmdir", "mv"}:
        return refs, targets, moves
    operands, recursive, target_directory = [], False, False
    options = True
    index = 0
    while index < len(args):
        token = args[index]
        index += 1
        if options and token == "--":
            options = False
        elif options and token.startswith("-"):
            if token == "--recursive" or (not token.startswith("--") and any(c in token[1:] for c in "rR")):
                recursive = True
            if name == "mv":
                if token in {"--target-directory", "--suffix"}:
                    target_directory |= token == "--target-directory"
                    index += 1
                elif token.startswith("--target-directory="):
                    target_directory = True
                elif not token.startswith("--"):
                    # GNU short options cluster. -t and -S consume the remaining
                    # token (or the next argv), so letters in their values are not flags.
                    for position, option in enumerate(token[1:], start=1):
                        if option in {"t", "S"}:
                            target_directory |= option == "t"
                            if position == len(token) - 1:
                                index += 1
                            break
        else:
            operands.append(token)
    if name == "rmdir" or (name == "rm" and recursive):
        targets.extend(operands)
    elif name == "mv":
        targets.extend(operands if target_directory else operands[:-1])
    return refs, targets, moves


def _python_paths(source: str, depth: int) -> tuple[list[str], list[str], list[str]]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return [], [], []  # invalid Python will not execute
    nodes = list(ast.walk(tree))
    if len(nodes) > 10000:
        raise ValueError("literal source exceeds knowledge scan bound")
    aliases = {}
    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"

    def qualified(node):
        if isinstance(node, ast.Name):
            return aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return f"{qualified(node.value)}.{node.attr}"
        return ""

    def path(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Call) and qualified(node.func) in {"pathlib.Path", "Path"}:
            if not node.args:
                return "."
            if len(node.args) == 1:
                return path(node.args[0])
        return None

    refs = [node.value for node in nodes if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    targets, moves = [], []
    destructive = {"shutil.rmtree", "shutil.move", "os.rename", "os.replace", "os.rmdir"}
    subprocess_calls = {"subprocess.run", "subprocess.call", "subprocess.check_call", "subprocess.check_output", "subprocess.Popen"}
    # ast.walk is breadth-first: a top-level relative chdir would precede an
    # earlier absolute chdir inside an if/with/try body. Preserve lexical source
    # order before the caller conservatively widens its possible cwd bases.
    # This is bounded literal analysis, not evaluation of branches, loops or
    # function invocation order; references still resolve against all bases.
    calls = sorted((node for node in nodes if isinstance(node, ast.Call)),
                   key=lambda node: (node.lineno, node.col_offset))
    for node in calls:
        name = qualified(node.func)
        operand = None
        if name == "os.chdir":
            # The Python spelling of ``cd``: aliases (``import os as o``,
            # ``from os import chdir``) and a literal ``Path(...)`` are followed.
            target_node = node.args[0] if node.args else next(
                (kw.value for kw in node.keywords if kw.arg == "path"), None)
            if target_node is not None and (move := path(target_node)) is not None:
                moves.append(move)
            continue
        if name in subprocess_calls:
            # A literal ``cwd=`` is a transition for that child's operands.
            for kw in node.keywords:
                if kw.arg == "cwd" and (move := path(kw.value)) is not None:
                    moves.append(move)
        if name in destructive:
            operand = node.args[0] if node.args else next(
                (kw.value for kw in node.keywords if kw.arg in {"path", "src"}), None)
        elif isinstance(node.func, ast.Attribute) and node.func.attr in {"rename", "replace", "rmdir"}:
            # A literal Path constructor, not an arbitrary object's method.
            if isinstance(node.func.value, ast.Call) and qualified(node.func.value.func) in {"pathlib.Path", "Path"}:
                operand = node.func.value
        if operand is not None and (target := path(operand)) is not None:
            targets.append(target)
        first = node.args[0] if node.args else next(
            (kw.value for kw in node.keywords if kw.arg == "args"), None)
        if first is None:
            continue
        if name in {"os.system", "os.popen", "exec", "eval"} and isinstance(first, ast.Constant) and isinstance(first.value, str):
            more_refs, more_targets, more_moves = literal_paths(first.value, python_source=name in {"exec", "eval"}, _depth=depth + 1)
            refs.extend(more_refs)
            targets.extend(more_targets)
            moves.extend(more_moves)
        elif name in subprocess_calls:
            if isinstance(first, (ast.List, ast.Tuple)) and all(isinstance(item, ast.Constant) and isinstance(item.value, str) for item in first.elts):
                more_refs, more_targets, more_moves = _shell_paths([item.value for item in first.elts], depth + 1)
                refs.extend(more_refs)
                targets.extend(more_targets)
                moves.extend(more_moves)
            elif isinstance(first, ast.Constant) and isinstance(first.value, str) and any(
                    kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True for kw in node.keywords):
                more_refs, more_targets, more_moves = literal_paths(first.value, _depth=depth + 1)
                refs.extend(more_refs)
                targets.extend(more_targets)
                moves.extend(more_moves)
    return refs, targets, moves
