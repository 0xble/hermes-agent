"""Bounded literal operands for the delegated knowledge boundary, not a shell jail.

Only known destructive commands/calls invert containment to protect ancestors.
Ordinary references retain the narrower protected-root/descendant policy.
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


def literal_paths(command: str, *, python_source: bool = False, _depth: int = 0) -> tuple[list[str], list[str]]:
    """Return literal references and destructive operands without evaluating code."""
    if _depth > 4:
        raise ValueError("nested literal source exceeds knowledge scan bound")
    if python_source:
        return _python_paths(command, _depth)
    references, destructive = [], []
    segment = []
    for token in [*shell_tokens(command), ";"]:
        if token and set(token) <= set(";&|()\n"):
            if segment:
                refs, targets = _shell_paths(segment, _depth)
                references.extend(refs)
                destructive.extend(targets)
                segment = []
        else:
            segment.append(token)
    return references, destructive


def _shell_paths(tokens: list[str], depth: int) -> tuple[list[str], list[str]]:
    refs, targets = list(tokens), []
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
        return refs, targets
    name, args = os.path.basename(words[0]), words[1:]
    if name in {"sh", "bash", "zsh", "dash", "ksh"} or re.fullmatch(r"python(?:\d+(?:\.\d+)*)?(?:\.exe)?", name):
        for index, option in enumerate(args[:-1]):
            if option == "-c":
                more_refs, more_targets = literal_paths(
                    args[index + 1], python_source=name.startswith("python"), _depth=depth + 1)
                refs.extend(more_refs)
                targets.extend(more_targets)
                break
    if name not in {"rm", "rmdir", "mv"}:
        return refs, targets
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
            if name == "mv" and token in {"-t", "--target-directory", "-S", "--suffix"}:
                target_directory |= token in {"-t", "--target-directory"}
                index += 1
            elif name == "mv" and token.startswith("--target-directory="):
                target_directory = True
        else:
            operands.append(token)
    if name == "rmdir" or (name == "rm" and recursive):
        targets.extend(operands)
    elif name == "mv":
        targets.extend(operands if target_directory else operands[:-1])
    return refs, targets


def _python_paths(source: str, depth: int) -> tuple[list[str], list[str]]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return [], []  # invalid Python will not execute
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
    targets = []
    destructive = {"shutil.rmtree", "shutil.move", "os.rename", "os.replace", "os.rmdir"}
    for node in nodes:
        if not isinstance(node, ast.Call):
            continue
        name = qualified(node.func)
        operand = None
        if name in destructive:
            operand = node.args[0] if node.args else next(
                (kw.value for kw in node.keywords if kw.arg in {"path", "src"}), None)
        elif isinstance(node.func, ast.Attribute) and node.func.attr in {"rename", "replace", "rmdir"}:
            # A literal Path constructor, not an arbitrary object's method.
            if isinstance(node.func.value, ast.Call) and qualified(node.func.value.func) in {"pathlib.Path", "Path"}:
                operand = node.func.value
        if operand is not None and (target := path(operand)) is not None:
            targets.append(target)
        if not node.args:
            continue
        first = node.args[0]
        if name in {"os.system", "os.popen", "exec", "eval"} and isinstance(first, ast.Constant) and isinstance(first.value, str):
            more_refs, more_targets = literal_paths(first.value, python_source=name in {"exec", "eval"}, _depth=depth + 1)
            refs.extend(more_refs)
            targets.extend(more_targets)
        elif name in {"subprocess.run", "subprocess.call", "subprocess.check_call", "subprocess.check_output", "subprocess.Popen"}:
            if isinstance(first, (ast.List, ast.Tuple)) and all(isinstance(item, ast.Constant) and isinstance(item.value, str) for item in first.elts):
                more_refs, more_targets = _shell_paths([item.value for item in first.elts], depth + 1)
                refs.extend(more_refs)
                targets.extend(more_targets)
            elif isinstance(first, ast.Constant) and isinstance(first.value, str) and any(
                    kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True for kw in node.keywords):
                more_refs, more_targets = literal_paths(first.value, _depth=depth + 1)
                refs.extend(more_refs)
                targets.extend(more_targets)
    return refs, targets
