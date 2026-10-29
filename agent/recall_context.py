"""Leftmost recall-note erasure with memoized immutable-suffix lookahead.

The input cursor only moves right. A retained-prefix stack permits a deletion
seam to complete an earlier note, without rescanning that prefix or the suffix.
Regex choices remain ordered, including greedy/lazy repeats and EOF assertions.
"""
from __future__ import annotations

import re
from functools import lru_cache
from re import _constants as C, _parser


# Instructions are (operation, argument, successor). A split's argument and
# successor are its first and second alternatives, in backtracking priority order.
class _Program:
    def __init__(self, patterns):
        self.nodes = []
        self.owners = []
        self.roots = []
        for owner, pattern in enumerate(patterns):
            if pattern.flags & ~(re.UNICODE | re.DOTALL):
                raise ValueError('Only Unicode and DOTALL recall regex flags are supported')
            parsed = _parser.parse(pattern.pattern, pattern.flags)
            if not parsed or parsed[0] != (C.LITERAL, ord('[')):
                raise ValueError('Recall notes must begin with a literal opening bracket')
            self.owner = owner
            accept = self.add('accept', None, None)
            self.roots.append(self.sequence(parsed, accept, bool(pattern.flags & re.DOTALL)))
        # Search only actual note openers, not every bracket in pasted code/JSON.
        # Derive them from the same program so registry edits cannot miss the guard.
        prefixes = set()
        pending = [(root, '', frozenset()) for root in self.roots]
        while pending:
            state, prefix, visited = pending.pop()
            operation, argument, successor = self.nodes[state]
            if state in visited:
                prefixes.add(prefix)
                continue
            visited = visited | {state}
            if operation == 'literal':
                pending.append((successor, prefix + argument, visited))
            elif operation == 'split':
                pending.extend((branch, prefix, visited) for branch in (argument, successor))
            else:
                prefixes.add(prefix)
        self.openers = re.compile('|'.join(map(re.escape, sorted(prefixes))))

    def add(self, operation, argument, successor):
        self.nodes.append((operation, argument, successor))
        self.owners.append(self.owner)
        return len(self.nodes) - 1

    def sequence(self, sequence, successor, dotall):
        for operation, value in reversed(sequence):
            if operation is C.LITERAL:
                successor = self.add('literal', chr(value), successor)
            elif operation is C.NOT_LITERAL:
                successor = self.add('not_literal', chr(value), successor)
            elif operation is C.ANY:
                successor = self.add('any', dotall, successor)
            elif operation is C.IN:
                negative = bool(value and value[0][0] is C.NEGATE)
                members = set()
                for kind, member in value[1:] if negative else value:
                    if kind is C.LITERAL:
                        members.add(chr(member))
                    elif kind is C.RANGE:
                        members.update(map(chr, range(member[0], member[1] + 1)))
                    else:
                        raise ValueError(f'Unsupported recall character class: {kind}')
                successor = self.add('class', (negative, frozenset(members)), successor)
            elif operation is C.SUBPATTERN:
                _group, add_flags, del_flags, body = value
                if add_flags or del_flags:
                    raise ValueError('Scoped recall regex flags are unsupported')
                successor = self.sequence(body, successor, dotall)
            elif operation is C.BRANCH:
                for branch in reversed(value[1]):
                    entry = self.sequence(branch, successor, dotall)
                    if branch is value[1][-1]:
                        alternatives = entry
                    else:
                        alternatives = self.add('split', entry, alternatives)
                successor = alternatives
            elif operation in (C.MAX_REPEAT, C.MIN_REPEAT):
                minimum, maximum, body = value
                if maximum == C.MAXREPEAT:
                    if body.getwidth()[0] == 0:
                        raise ValueError('Unbounded recall repeats must consume input')
                    split = self.add('split', None, None)
                    repeated = self.sequence(body, split, dotall)
                    self.nodes[split] = ('split', repeated, successor) if operation is C.MAX_REPEAT else (
                        'split', successor, repeated)
                    successor = split
                else:
                    for _ in range(maximum - minimum):
                        repeated = self.sequence(body, successor, dotall)
                        successor = self.add('split', repeated, successor) if operation is C.MAX_REPEAT else (
                            self.add('split', successor, repeated))
                for _ in range(minimum):
                    successor = self.sequence(body, successor, dotall)
            elif operation is C.ASSERT:
                direction, body = value
                expected = _parser.parse(r'\n|$', 0)
                if direction != 1 or repr(body) != repr(expected):
                    raise ValueError('Only the recall newline-or-EOF lookahead is supported')
                successor = self.add('eol', None, successor)
            else:
                raise ValueError(f'Unsupported recall regex operation: {operation}')
        return successor


def _consumes(operation, argument, char):
    if operation == 'literal':
        return char == argument
    if operation == 'not_literal':
        return char != argument
    if operation == 'any':
        return argument or char != '\n'
    if operation == 'class':
        negative, members = argument
        return (char in members) != negative
    return False


class _Suffix:
    def __init__(self, program, text, limit):
        self.program = program
        self.text = text
        self.limit = limit
        self.cache = [{} for _ in program.nodes]

    def match(self, state, position):
        """First successful endpoint, evaluating each (state, position) at most once."""
        stack = [(state, position)]
        nodes, cache = self.program.nodes, self.cache
        while stack:
            current, index = stack[-1]
            if index in cache[current]:
                stack.pop()
                continue
            operation, argument, successor = nodes[current]
            if operation == 'accept':
                cache[current][index] = index
            elif operation == 'split':
                if index not in cache[argument]:
                    stack.append((argument, index))
                    continue
                if cache[argument][index] >= 0:
                    cache[current][index] = cache[argument][index]
                elif index not in cache[successor]:
                    stack.append((successor, index))
                    continue
                else:
                    cache[current][index] = cache[successor][index]
            else:
                if operation == 'eol':
                    viable = index == self.limit or self.text[index] == '\n'
                    following = index
                else:
                    viable = index < self.limit and _consumes(operation, argument, self.text[index])
                    following = index + 1
                if not viable:
                    cache[current][index] = -1
                elif following not in cache[successor]:
                    stack.append((successor, following))
                    continue
                else:
                    cache[current][index] = cache[successor][following]
            stack.pop()
        return cache[state][position]

    def search(self, position):
        while position < self.limit:
            position = self.text.find('[', position, self.limit)
            if position < 0:
                break
            best = None
            for owner, root in enumerate(self.program.roots):
                end = self.match(root, position)
                if end >= 0:
                    best = position, end, owner
                    break
            if best is not None:
                return best
            position += 1
        return None


def _advance(program, states, char, position):
    seeds = list(states)
    if char == '[':
        seeds.extend((root, position) for root in program.roots)
    pending = list(reversed(seeds))
    seen, consumed, result = set(), set(), []
    while pending:
        state, start = pending.pop()
        if state in seen:
            continue
        seen.add(state)
        operation, argument, successor = program.nodes[state]
        if operation == 'split':
            pending.extend(((successor, start), (argument, start)))
        elif operation == 'eol':
            if char == '\n':
                pending.append((successor, start))
        elif _consumes(operation, argument, char) and successor not in consumed:
            consumed.add(successor)
            result.append((successor, start))
    return result


@lru_cache(maxsize=1)
def _program(patterns):
    return _Program(patterns)


def _regex_strip(text, patterns, openers):
    """Try each original regex once per opener, never search a failed suffix twice.

    Complete disjoint notes need no per-character state. A failed opener or a
    possible deletion seam uses the bounded engine on the original input. After
    the first deletion, regex matching sees the stripped EOF, just as before.
    """
    cursor, limit = 0, len(text)
    parts, descriptions = [], []
    while opener := openers.search(text, cursor, limit):
        match = next((match for pattern in patterns
                      if (match := pattern.match(text, opener.start(), limit))), None)
        if match is None:
            return None
        parts.append(text[cursor:match.start()])
        if 'description' in match.re.groupindex and (description := match['description'].strip()):
            descriptions.append(description)
        cursor = match.end()
        if len(parts) == 1:
            limit = len(text.rstrip())
    if not parts:
        return text, []
    parts.append(text[cursor:limit])
    result = ''.join(parts).strip()
    if openers.search(result):
        return None
    return result, descriptions


def strip_recall_context(
    text: str, patterns: tuple[re.Pattern[str], ...],
) -> tuple[str, list[str]]:
    program = _program(patterns)
    fast = _regex_strip(text, patterns, program.openers)
    if fast is not None:
        return fast
    return _strip_bounded(text, patterns, program)


def _strip_bounded(text, patterns, program):
    """Erase notes in original leftmost order with O(pattern states × input) work.

    There are 2 suffix tables at most, before and after the first whole-turn
    strip changes EOF. The retained-prefix snapshots are pushed or popped once
    per character. Even malformed openers and notes created by deletion seams
    therefore cannot trigger suffix rescanning or repeated whole-turn copies.
    """
    suffix = _Suffix(program, text, len(text))
    cursor, limit = 0, len(text)
    chars, snapshots = [], [[]]
    descriptions = []
    removed = False
    while True:
        crossing = None
        for state, start in snapshots[-1]:
            end = suffix.match(state, cursor)
            if end >= 0:
                crossing = start, end, program.owners[state]
                break
        if crossing is not None:
            start, end, owner = crossing
            matched_text = ''.join(chars[start:]) + text[cursor:end]
            del chars[start:]
            del snapshots[start + 1:]
        else:
            following = suffix.search(cursor)
            if following is None:
                if not removed:
                    return text, []
                return (''.join(chars) + text[cursor:limit]).strip(), descriptions
            start, end, owner = following
            for char in text[cursor:start]:
                if not chars and char.isspace():
                    continue
                snapshots.append(_advance(program, snapshots[-1], char, len(chars)))
                chars.append(char)
            matched_text = text[start:end]
        original = patterns[owner].fullmatch(matched_text)
        assert original is not None, (owner, matched_text)
        if 'description' in original.re.groupindex:
            description = original['description'].strip()
            if description:
                descriptions.append(description)
        if not removed:
            removed = True
            limit = len(text.rstrip())
            # Whole-turn stripping changes EOF once, not once per removed note.
            suffix = _Suffix(program, text, limit)
        cursor = min(end, limit)
        if not chars:
            while cursor < limit and text[cursor].isspace():
                cursor += 1
        if cursor == limit:
            while chars and chars[-1].isspace():
                chars.pop()
                snapshots.pop()
