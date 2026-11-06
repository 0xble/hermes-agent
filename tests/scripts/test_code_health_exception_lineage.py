"""Pure checker regressions: synthetic source and explicit RuffFile data, no external tools.

The supplied BLE001/F811 diagnostics are measurement input, not evidence of a Ruff run.
"""
import ast
from pathlib import Path

import pytest

from scripts.code_health.compare import compare
from scripts.code_health.gitio import Change
from scripts.code_health.measure import Measurer
from scripts.code_health.model import FileMeasure
from scripts.code_health.ruff_runner import RuffFile


def measured(text, hits=None, path="pkg/lifecycle.py"):
    tree = ast.parse(text)
    if hits is None:
        hits = [("BLE001", n.lineno) for n in ast.walk(tree)
                if isinstance(n, ast.ExceptHandler) and isinstance(n.type, ast.Name)
                and n.type.id == "Exception"]
    fm = FileMeasure(path, lines=text.splitlines())
    Measurer(Path(__file__).resolve().parents[2], [], known_env=set())._python(fm, text, RuffFile(hits=hits))
    assert fm.error is None
    return fm


_HANDOFF = """import asyncio

class Owner:
    def publish(self, error):
        self.failure = error
        self.ready.set()

    async def run(self, config):
        try:
            await self.connect(config)
        except Exception as exc:
            self.publish(exc)

    async def start(self, config):
        self.task = asyncio.create_task(self.run(config))
        try:
            await self.ready.wait()
        except asyncio.CancelledError:
            self.task.cancel()
            raise
        if self.failure:
            raise self.failure
"""


@pytest.mark.parametrize("old,new,flagged", [
    ("", "", False),
    ("self.failure = error", "self.failure = None", True),
    ("self.publish(exc)", "self.publish(RuntimeError('replacement'))", True),
    ("self.ready.set()", "self.other.set()", True),
    ("await self.ready.wait()", "await self.other.wait()", True),
    ("raise self.failure", "return self.failure", True),
    ("self.failure = error", "self.failure = str(error)", True),
    ("self.failure = error", "pass", True),
    ("self.run(config)", "self.other(config)", True),
    ("        if self.failure:", "        self.failure = None\n        if self.failure:", True),
    ("self.ready.set()", "pass", True),
    ("self.publish(exc)", "pass", True),
    ("self.publish(exc)", "self.publish(exc)\n            self.failure = None", True),
    ("async def start", "async def disconnected", False),  # identity is structural, not named
    ("            self.publish(exc)", "            self.publish(exc)\n        finally:\n            self.failure = None", True),
    ("    def publish", "    async def publish", True),
    ("    async def start(self, config):", "    async def start(self, config):\n        return", True),
    ("    async def start(self, config):", "    async def start(self, config):\n        raise RuntimeError('stopped')", True),
    ("    async def start(self, config):", "    async def start(self, config):\n        if config:\n            return", True),
    ("    async def run(self, config):\n        try:\n            await self.connect(config)",
     "    def run(self, config):\n        try:\n            self.connect(config)", True),
    ("            await self.connect(config)", "            yield config", True),
    ("    async def start(self, config):", "    async def start(self, config):\n        yield config", True),
    ("            self.publish(exc)", "            self.publish(exc)\n        finally:\n            self.publish(None)", True),
    ("            self.publish(exc)", "            self.publish(exc)\n        finally:\n            self.reset()", True),
    ("            self.publish(exc)", "            self.publish(exc)\n        finally:\n            self.ready.clear()", True),
])
def test_exact_same_object_lifecycle_forwarding(old, new, flagged):
    fm = measured(_HANDOFF.replace(old, new) if old else _HANDOFF)
    assert any(hit.rule == "BLE001" for hit in fm.hits) is flagged


def test_missing_reader_still_blocks():
    text = _HANDOFF[:_HANDOFF.index("    async def start")]
    assert any(hit.rule == "BLE001" for hit in measured(text).hits)


@pytest.mark.parametrize("rule", ["F811", "BLE001", "S110"])
def test_unrelated_explicit_diagnostics_are_never_blanket_suppressed(rule):
    fm = measured("def unrelated():\n    pass\n", hits=[(rule, 2)])
    assert any(hit.rule == rule for hit in fm.hits)


_EXTRACT_BASE = '''class Handler:
    def dispatch(self, value):
        """Old description."""
        argument = self.prepare(value)
        try:
            result = self.invoke(argument)
        except Exception as exc:
            return self.fail(str(exc))
        return self.finish(result)
'''
_EXTRACT_WRAPPER = """class Handler:
    def dispatch(self, value):
        return self.sample(value)

"""
_EXTRACT_HELPER = _EXTRACT_BASE.split("class Handler:\n", 1)[1].replace(
    "dispatch", "sample").replace("Old description.", "New description.")


@pytest.mark.parametrize("helper,wrapper,expected", [
    (_EXTRACT_HELPER, _EXTRACT_WRAPPER, 0),
    (_EXTRACT_HELPER.replace('        argument =', '        if value is None:\n            return None\n        argument ='),
     _EXTRACT_WRAPPER, 0),
    (_EXTRACT_HELPER.replace("self.fail(str(exc))", "None"), _EXTRACT_WRAPPER, 1),
    (_EXTRACT_HELPER, _EXTRACT_BASE + "\n", 1),
    (_EXTRACT_HELPER + "\n" + _EXTRACT_HELPER.replace("sample", "copy"), _EXTRACT_WRAPPER, 2),
    (_EXTRACT_HELPER, "class Handler:\n    def dispatch(self, value):\n        return value\n\n", 1),
    (_EXTRACT_HELPER.replace("self.invoke(argument)", "self.unrelated(argument)"), _EXTRACT_WRAPPER, 1),
    (_EXTRACT_HELPER.replace("self.fail(str(exc))", "other.fail(str(exc))"), _EXTRACT_WRAPPER, 1),
    (_EXTRACT_HELPER, _EXTRACT_WRAPPER.replace("self.sample", "other.sample"), 1),
    (_EXTRACT_HELPER.replace("        argument = self.prepare(value)\n", "").replace(
        "        return self.finish(result)", "        argument = self.prepare(value)\n        return self.finish(result)"),
     _EXTRACT_WRAPPER, 1),
    (_EXTRACT_HELPER + "\n" + _EXTRACT_HELPER.replace("sample", "sample"), _EXTRACT_WRAPPER, 2),
    (_EXTRACT_HELPER.replace("        return self.finish(result)", "        try:\n            self.added(value)\n        except Exception:\n            pass\n        return self.finish(result)"), _EXTRACT_WRAPPER, 1),
])
def test_one_surviving_same_origin_exception_block(helper, wrapper, expected):
    base = measured(_EXTRACT_BASE)
    head = measured(wrapper + helper)
    findings = compare({base.path: base}, {head.path: head}, [Change("M", base.path, head.path)])
    assert len([f for f in findings if f.rule == "BLE001"]) == expected


def test_extraction_cannot_move_numeric_caps_or_spend_old_debt_twice():
    base = measured(_EXTRACT_BASE)
    head = measured(_EXTRACT_WRAPPER + _EXTRACT_HELPER)
    base.units['Handler.dispatch'].metrics['CC'] = 42
    head.units['Handler.sample'].metrics['CC'] = 21
    findings = compare({base.path: base}, {head.path: head}, [Change("M", base.path, head.path)])
    assert [(f.rule, f.scope) for f in findings] == [('CC', 'Handler.sample')]


def test_ambiguous_repeated_origins_do_not_pay_for_an_extraction():
    base = measured(_EXTRACT_BASE + _EXTRACT_BASE.split("class Handler:\n", 1)[1].replace(
        "self.invoke(argument)", "self.unrelated(argument)"))
    head = measured(_EXTRACT_WRAPPER + _EXTRACT_HELPER)
    findings = compare({base.path: base}, {head.path: head}, [Change("M", base.path, head.path)])
    assert len([f for f in findings if f.rule == 'BLE001']) == 1


@pytest.mark.parametrize("mutator", [
    "self.failure = None",
    "self.publish(None)",
    "self.ready.clear()",
    "self.clear_failure()",
])
def test_post_handler_resolved_mutators_cannot_erase_delivery(mutator):
    text = _HANDOFF.replace("            self.publish(exc)",
        "            self.publish(exc)\n        finally:\n            self.reset()")
    text += "\n    def reset(self):\n        " + mutator + "\n"
    text += "\n    def clear_failure(self):\n        self.failure = None\n"
    assert any(hit.rule == "BLE001" for hit in measured(text).hits)


@pytest.mark.parametrize("rule", ["BLE001", "S110"])
@pytest.mark.parametrize("expression,consumer", [
    ("len([self.sample])", ""),
    ("tuple((self.sample,))", ""),
    ("self.replay(lambda: self.sample(value), value)", ""),
    ("self.replay(self.sample, value)", ""),
    ("self.replay(self.sample, value)", "    def replay(self, callback, value):\n        return len([callback])\n"),
    ("self.replay(self.sample, value)", "    def replay(self, callback, value):\n        return value\n        return callback(value)\n"),
    ("self.replay(self.sample, value)", "    def replay(self, callback, value):\n        callback = self.other\n        return callback(value)\n"),
])
def test_disconnected_helper_references_remain_new_debt(rule, expression, consumer):
    base = measured(_EXTRACT_BASE)
    source = _EXTRACT_WRAPPER.replace("self.sample(value)", expression) + consumer + _EXTRACT_HELPER
    head = measured(source)
    # Explicit diagnostics are inputs to both real measurement and comparison.
    for fm in (base, head):
        rows = [(rule, n.lineno) for n in ast.walk(ast.parse("\n".join(fm.lines)))
                if isinstance(n, ast.ExceptHandler)]
        updated = measured("\n".join(fm.lines), hits=rows)
        if fm is base:
            base = updated
        else:
            head = updated
    findings = compare({base.path: base}, {head.path: head}, [Change("M", base.path, head.path)])
    assert len([f for f in findings if f.rule == rule]) == 1


@pytest.mark.parametrize("rule", ["BLE001", "S110"])
@pytest.mark.parametrize("body,expected", [
    ("return callback(value)", 0),
    ("return len([callback])", 1),
])
def test_resolved_local_callback_consumer_must_invoke_its_bound_argument(rule, body, expected):
    wrapper = _EXTRACT_WRAPPER.replace("self.sample(value)", "self.replay(self.sample, value)")
    wrapper += "    def replay(self, callback, value):\n        " + body + "\n"
    base = measured(_EXTRACT_BASE, hits=[(rule, 7)])
    source = wrapper + _EXTRACT_HELPER
    rows = [(rule, n.lineno) for n in ast.walk(ast.parse(source)) if isinstance(n, ast.ExceptHandler)]
    head = measured(source, hits=rows)
    findings = compare({base.path: base}, {head.path: head}, [Change("M", base.path, head.path)])
    assert len([f for f in findings if f.rule == rule]) == expected


_CALLBACK_MODULE = """import asyncio
import contextvars

async def run_owned(call_context, callback, *args, on_unowned=lambda: None, private_allowed=True):
    captured = contextvars.copy_context() if call_context is None else call_context()
    if captured is None:
        return on_unowned()
    if not private_allowed:
        from agent.incognito import is_incognito
        if captured.run(is_incognito):
            return on_unowned()
    return await captured.copy().run(asyncio.create_task, callback(*args))
"""


@pytest.mark.parametrize("mutation,expected", [
    (None, 0),
    (("callback(*args)", "args"), 1),
    (("callback(*args)", "lambda: callback(*args)"), 1),
    (("    return await captured", "    callback = on_unowned\n    return await captured"), 1),
    (("    return await captured", "    return on_unowned()\n    return await captured"), 1),
])
def test_imported_callback_credit_requires_unique_measured_invoking_source(mutation, expected):
    wrapper = """class Handler:
    async def dispatch(self, value):
        from pkg.callback_owner import run_owned as replay
        return await replay(self.context, self.sample, value, on_unowned=lambda: None)

"""
    helper = _EXTRACT_HELPER.replace("    def sample", "    async def sample")
    base = measured(_EXTRACT_BASE.replace("    def dispatch", "    async def dispatch"))
    head = measured(wrapper + helper)
    source = _CALLBACK_MODULE.replace(*mutation) if mutation else _CALLBACK_MODULE
    callback = measured(source, hits=[], path="pkg/callback_owner.py")
    findings = compare({base.path: base, callback.path: callback},
        {head.path: head, callback.path: callback}, [Change("M", base.path, head.path)])
    assert len([f for f in findings if f.rule == "BLE001"]) == expected


def test_imported_callback_without_measured_source_still_blocks():
    wrapper = """class Handler:
    def dispatch(self, value):
        from pkg.unavailable import replay
        return replay(self.sample, value)

"""
    base, head = measured(_EXTRACT_BASE), measured(wrapper + _EXTRACT_HELPER)
    findings = compare({base.path: base}, {head.path: head}, [Change("M", base.path, head.path)])
    assert len([f for f in findings if f.rule == "BLE001"]) == 1


@pytest.mark.parametrize("old,new", [
    ("import asyncio", "import asyncio\nasyncio = replacement"),
    ("    async def start(self, config):", "    async def start(self, config):\n        asyncio = replacement"),
])
def test_shadowed_task_scheduler_is_not_delivery_evidence(old, new):
    assert any(hit.rule == "BLE001" for hit in measured(_HANDOFF.replace(old, new)).hits)


def test_actual_mcp_startup_handoff_keeps_its_existing_exception_behavior():
    # Product source is checker INPUT, not a source-shape assertion or imported runtime.
    path = "tools/mcp_tool_server_run.py"
    source = (Path(__file__).resolve().parents[2] / path).read_text()
    producer = next(n for n in ast.walk(ast.parse(source))
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == "run")
    handler = next(n for n in ast.walk(producer) if isinstance(n, ast.ExceptHandler)
                   and n.name == "exc" and len(n.body) == 1)
    fm = measured(source, hits=[("BLE001", handler.lineno)], path=path)
    assert not any(hit.rule == "BLE001" for hit in fm.hits)


@pytest.mark.parametrize("rule", ["BLE001", "S110"])
def test_actual_owned_callback_source_preserves_invoking_extraction(rule):
    path = "tools/mcp_tool_callback_context.py"
    source = (Path(__file__).resolve().parents[2] / path).read_text()
    callback = measured(source, hits=[], path=path)
    wrapper = """class Handler:
    async def dispatch(self, value):
        from tools.mcp_tool_callback_context import run_owned_callback as replay
        return await replay(self.context, self.sample, value, on_unowned=lambda: None)

"""
    base = measured(_EXTRACT_BASE.replace("    def dispatch", "    async def dispatch"), hits=[(rule, 7)])
    head_source = wrapper + _EXTRACT_HELPER.replace("    def sample", "    async def sample")
    head = measured(head_source, hits=[(rule, n.lineno) for n in ast.walk(ast.parse(head_source))
                                      if isinstance(n, ast.ExceptHandler)])
    findings = compare({base.path: base, callback.path: callback},
        {head.path: head, callback.path: callback}, [Change("M", base.path, head.path)])
    assert not [f for f in findings if f.rule == rule]


@pytest.mark.parametrize("body", [
    "            self.publish(None)",
    "            self.reset()",
    "            self.ready.clear()",
])
def test_post_handler_fallthrough_cannot_destroy_delivery(body):
    source = _HANDOFF.replace("            self.publish(exc)", "            self.publish(exc)\n" + body[4:])
    source += "\n    def reset(self):\n        self.failure = None\n"
    assert any(hit.rule == "BLE001" for hit in measured(source).hits)


@pytest.mark.parametrize("consumer", [
    "    async def replay(self, callback, value):\n        return callback(value)\n",
    "    def replay(self, callback, value):\n        yield value\n        return callback(value)\n",
    "    def replay(self, callback, value):\n        if callback:\n            return value\n        return callback(value)\n",
])
def test_callback_consumer_must_execute_rather_than_return_or_defer_a_coroutine(consumer):
    wrapper = _EXTRACT_WRAPPER.replace("self.sample(value)", "self.replay(self.sample, value)")
    base, head = measured(_EXTRACT_BASE), measured(wrapper + consumer + _EXTRACT_HELPER)
    findings = compare({base.path: base}, {head.path: head}, [Change("M", base.path, head.path)])
    assert len([f for f in findings if f.rule == "BLE001"]) == 1


def test_generator_helper_reference_is_not_an_executed_extraction():
    source = _EXTRACT_WRAPPER + _EXTRACT_HELPER.replace("        try:", "        yield value\n        try:")
    base, head = measured(_EXTRACT_BASE), measured(source)
    findings = compare({base.path: base}, {head.path: head}, [Change("M", base.path, head.path)])
    assert len([f for f in findings if f.rule == "BLE001"]) == 1


@pytest.mark.parametrize("producer", [
    """    async def run(self, config):
        for attempt in range(2):
            self.failure = None
            try:
                if attempt == 0:
                    raise RuntimeError('first failure')
            except Exception as exc:
                self.publish(exc)
""",
    """    async def run(self, config):
        attempt = 0
        while attempt < 2:
            self.ready.clear()
            try:
                if attempt == 0:
                    raise RuntimeError('first failure')
            except Exception as exc:
                self.publish(exc)
            attempt += 1
            continue
""",
    """    async def run(self, config):
        for attempt in range(1):
            try:
                raise RuntimeError('first failure')
            except Exception as exc:
                self.publish(exc)
        else:
            self.failure = None
""",
    """    async def run(self, config):
        for attempt in range(2):
            try:
                self.failure = None
                try:
                    if attempt == 0:
                        raise RuntimeError('first failure')
                except Exception as exc:
                    self.publish(exc)
            finally:
                config = None
""",
], ids=["for-backedge", "while-continue", "loop-else", "nested-finally-backedge"])
def test_loop_continuations_cannot_erase_published_exception(producer):
    prefix, tail = _HANDOFF.split("    async def run", 1)
    reader = "    async def start" + tail.split("    async def start", 1)[1]
    assert any(hit.rule == "BLE001" for hit in measured(prefix + producer + "\n" + reader).hits)


@pytest.mark.parametrize("rule", ["BLE001", "S110"])
@pytest.mark.parametrize("kind", [
    "generator", "yield-from", "async-generator", "decorated", "coroutine-unawaited",
    "coroutine-awaited", "nested-generator", "wrapper-decorated",
])
def test_callback_credit_requires_executing_the_helpers_body(rule, kind):
    base_source, helper = _EXTRACT_BASE, _EXTRACT_HELPER
    wrapper = _EXTRACT_WRAPPER.replace("self.sample(value)", "self.replay(self.sample, value)")
    consumer = "    def replay(self, callback, value):\n        return callback(value)\n"
    if kind in {"generator", "yield-from", "async-generator"}:
        suspension = "yield from (value,)" if kind == "yield-from" else "yield value"
        helper = helper.replace("        try:", "        " + suspension + "\n        try:")
    if kind == "async-generator":
        # Async generators cannot return values; preserve the same legal catch in both trees.
        for original in ("return self.fail(str(exc))", "return self.finish(result)"):
            base_source = base_source.replace(original, original.removeprefix("return "))
            helper = helper.replace(original, original.removeprefix("return "))
        helper = helper.replace("    def sample", "    async def sample")
    elif kind.startswith("coroutine"):
        helper = helper.replace("    def sample", "    async def sample")
        if kind == "coroutine-awaited":
            wrapper = wrapper.replace("    def dispatch", "    async def dispatch").replace(
                "return self.replay", "return await self.replay")
            consumer = "    async def replay(self, callback, value):\n        return await callback(value)\n"
    elif kind == "decorated":
        helper = helper.replace("    def sample", "    @defer\n    def sample")
    elif kind == "nested-generator":
        helper = helper.replace("        try:", "        def unrelated():\n            yield value\n        try:")
    elif kind == "wrapper-decorated":
        wrapper = wrapper.replace("    def dispatch", "    @defer\n    def dispatch")
    base = measured(base_source, hits=[(rule, 7)])
    source = wrapper + consumer + helper
    head = measured(source, hits=[(rule, n.lineno) for n in ast.walk(ast.parse(source))
                                 if isinstance(n, ast.ExceptHandler)])
    findings = compare({base.path: base}, {head.path: head}, [Change("M", base.path, head.path)])
    expected = 0 if kind in {"coroutine-awaited", "nested-generator"} else 1
    assert len([f for f in findings if f.rule == rule]) == expected


@pytest.mark.parametrize("rule", ["BLE001", "S110"])
@pytest.mark.parametrize("binding", [
    "module", "local", "local-over-parameter", "ordinary-parameter", "positional-only",
    "keyword-only", "variadic", "keyword-variadic", "module-assignment", "module-import",
    "module-function", "module-class", "module-conditional", "late-local-import",
    "local-assignment", "global-declaration", "closure-parameter", "module-star",
    "export-rebinding", "late-local-imports",
])
def test_imported_consumer_credit_requires_the_actual_lexical_binding(rule, binding):
    module = "from pkg.callback_owner import replay\n\n"
    wrapper = _EXTRACT_WRAPPER.replace("self.sample(value)", "replay(self.sample, value)")
    signature = "def dispatch(self, value)"
    signatures = {
        "ordinary-parameter": "def dispatch(self, value, replay=lambda callback, value: value)",
        "positional-only": "def dispatch(self, value, replay, /)",
        "keyword-only": "def dispatch(self, value, *, replay)",
        "variadic": "def dispatch(self, value, *replay)",
        "keyword-variadic": "def dispatch(self, value, **replay)",
        "local-over-parameter": "def dispatch(self, value, replay=None)",
    }
    wrapper = wrapper.replace(signature, signatures.get(binding, signature))
    if binding in {"local", "local-over-parameter"}:
        module = ""
        wrapper = wrapper.replace("        return replay", "        from pkg.callback_owner import replay\n        return replay")
    elif binding in {"late-local-import", "late-local-imports"}:
        module = ""
        wrapper = wrapper.rstrip() + "\n        from pkg.callback_owner import replay\n\n"
        if binding == "late-local-imports":
            wrapper = wrapper.rstrip() + "\n        import other\n\n"
    elif binding == "local-assignment":
        wrapper = wrapper.replace("        return replay", "        replay = lambda callback, value: value\n        return replay")
    elif binding == "global-declaration":
        wrapper = wrapper.replace("        return replay", "        global replay\n        return replay")
    elif binding.startswith("module-"):
        module += {
            "module-assignment": "replay = lambda callback, value: value\n",
            "module-import": "import other as replay\n",
            "module-function": "def replay(callback, value):\n    return value\n",
            "module-class": "class replay:\n    pass\n",
            "module-conditional": "if replacement:\n    replay = replacement\n",
            "module-star": "from other import *\n",
        }[binding] + "\n"
    source = module + wrapper + _EXTRACT_HELPER
    if binding == "closure-parameter":
        source = module + "def enclosing(replay):\n" + "\n".join(
            "    " + line if line else "" for line in (wrapper + _EXTRACT_HELPER).splitlines()) + "\n"
        base_source = "def enclosing(replay):\n" + "\n".join(
            "    " + line if line else "" for line in _EXTRACT_BASE.splitlines()) + "\n"
    else:
        base_source = _EXTRACT_BASE
    base = measured(base_source, hits=[(rule, n.lineno) for n in ast.walk(ast.parse(base_source))
                                      if isinstance(n, ast.ExceptHandler)])
    head = measured(source, hits=[(rule, n.lineno) for n in ast.walk(ast.parse(source))
                                 if isinstance(n, ast.ExceptHandler)])
    callback_source = "def replay(callback, value):\n    return callback(value)\n"
    if binding == "export-rebinding":
        callback_source += "replay = lambda callback, value: value\n"
    callback = measured(callback_source,
                        hits=[], path="pkg/callback_owner.py")
    findings = compare({base.path: base, callback.path: callback},
                       {head.path: head, callback.path: callback}, [Change("M", base.path, head.path)])
    expected = 0 if binding in {"module", "local", "local-over-parameter"} else 1
    assert len([f for f in findings if f.rule == rule]) == expected


@pytest.mark.parametrize("rule", ["BLE001", "S110"])
@pytest.mark.parametrize("shadow", ["asyncio", "contextvars"])
def test_imported_callback_scheduler_must_resolve_its_lexical_module(rule, shadow):
    wrapper = """class Handler:
    async def dispatch(self, value):
        from pkg.callback_owner import run_owned as replay
        return await replay(self.context, self.sample, value, on_unowned=lambda: None)

"""
    base = measured(_EXTRACT_BASE.replace("    def dispatch", "    async def dispatch"), hits=[(rule, 7)])
    source = wrapper + _EXTRACT_HELPER.replace("    def sample", "    async def sample")
    head = measured(source, hits=[(rule, n.lineno) for n in ast.walk(ast.parse(source))
                                 if isinstance(n, ast.ExceptHandler)])
    callback = measured(_CALLBACK_MODULE.replace("private_allowed=True):", "private_allowed=True, " + shadow + "=None):"),
                        hits=[], path="pkg/callback_owner.py")
    findings = compare({base.path: base, callback.path: callback},
                       {head.path: head, callback.path: callback}, [Change("M", base.path, head.path)])
    assert len([f for f in findings if f.rule == rule]) == 1


# Verbatim callback methods from tools/mcp_tool_sampling.py at the comparison base
# 64bdd2d1e87a85645ac292cbfbc6e6e6d9fc0afa. No Git/runtime import in the test.
_PRODUCTION_CALLBACK_BASE = '''class SamplingHandler:
    async def __call__(self, context, params):
        """SDK ``SamplingFnT``: CreateMessageResult, CreateMessageResultWithTools, or ErrorData."""
        resolved_model, err = self._admit(params)
        if err is not None:
            return err
        sync_call = self._build_llm_call(params, resolved_model)  # outside the try: its errors propagate, not _fail
        try:
            response = await asyncio.wait_for(asyncio.to_thread(sync_call), timeout=self.timeout)
        except TimeoutError:
            return self._fail(f"Sampling LLM call timed out after {self.timeout}s for server '{self.server_name}'")
        except Exception as exc:
            return self._fail(f"Sampling LLM call failed: {_sanitize_error(_exc_str(exc))}")
        # Empty choices happen on content filtering / provider errors.
        if not getattr(response, "choices", None):
            return self._fail(f"LLM returned empty response (no choices) for server '{self.server_name}'")
        choice = response.choices[0]
        self.metrics["requests"] += 1
        total_tokens = getattr(getattr(response, "usage", None), "total_tokens", 0)
        self.metrics["tokens_used"] += total_tokens if isinstance(total_tokens, int) else 0
        if choice.finish_reason == "tool_calls" and getattr(choice.message, "tool_calls", None):
            return self._build_tool_use_result(choice, response)
        return self._build_text_result(choice, response)

class ElicitationHandler:
    async def __call__(self, context, params):
        """SDK elicitation callback (``ElicitationFnT``). Returns ElicitResult or ErrorData."""
        self.metrics["requests"] += 1
        if getattr(params, "mode", "form") == "url":  # OAuth/payment: needs a browser + elicitation/complete; unsupported
            logger.info("MCP server '%s' requested URL-mode elicitation; declining "
                        "(URL-mode elicitation not implemented)", self.server_name)
            return self._result("decline", "declined")

        message = getattr(params, "message", "") or f"MCP server '{self.server_name}' is requesting your approval"
        # ``requestedSchema`` on mcp 1.x, ``requested_schema`` on 2.0 (aliases don't apply to attribute
        # access) — read both or the user approves without seeing the fields.
        schema = getattr(params, "requestedSchema", None) or getattr(params, "requested_schema", None) or {}
        logger.info("MCP server '%s' elicitation request: %s", self.server_name, _sanitize_error(message)[:200])
        try:  # lazy import inside avoids import-order coupling with early-bootstrap tools.approval
            invoke_consent = self._consent_thunk(message, _format_elicitation_schema_summary(schema, self.server_name))
        except Exception as exc:  # pragma: no cover -- defensive
            logger.error("MCP server '%s' elicitation: approval system unavailable: %s", self.server_name, exc)
            return self._result("decline", "errors")
        try:  # off-thread: inline, the sync consent flow would freeze the MCP loop and every RPC on it
            answer = await asyncio.wait_for(
                asyncio.to_thread(invoke_consent), timeout=self.timeout + self._OUTER_TIMEOUT_GRACE_SECONDS)
        except TimeoutError:
            logger.warning("MCP server '%s' elicitation timed out after %ds", self.server_name, int(self.timeout))
            return self._result("cancel", "errors")
        except Exception as exc:
            logger.error("MCP server '%s' elicitation failed: %s", self.server_name, exc, exc_info=True)
            return self._result("decline", "errors")
        return self._result(*self._ANSWER_RESULTS.get(answer, ("decline", "declined")))
'''


@pytest.mark.parametrize("rule", ["BLE001", "S110"])
@pytest.mark.parametrize("owner,helper", [
    ("SamplingHandler", "_sample"), ("ElicitationHandler", "_elicit"),
])
@pytest.mark.parametrize("consumer_available", [True, False])
def test_documented_production_callbacks_preserve_the_base_extraction(rule, owner, helper, consumer_available):
    root = Path(__file__).resolve().parents[2]
    path = "tools/mcp_tool_sampling.py"
    source = (root / path).read_text()

    def exception_rows(text, method):
        cls = next(n for n in ast.parse(text).body if isinstance(n, ast.ClassDef) and n.name == owner)
        function = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == method)
        return [(rule, n.lineno) for n in ast.walk(function) if isinstance(n, ast.ExceptHandler)
                and isinstance(n.type, ast.Name) and n.type.id == "Exception"]

    base = measured(_PRODUCTION_CALLBACK_BASE, hits=exception_rows(_PRODUCTION_CALLBACK_BASE, "__call__"), path=path)
    rows = exception_rows(source, helper)
    head = measured(source, hits=rows, path=path)
    before, after = {path: base}, {path: head}
    if consumer_available:
        callback_path = "tools/mcp_tool_callback_context.py"
        callback = measured((root / callback_path).read_text(), hits=[], path=callback_path)
        before[callback_path] = after[callback_path] = callback
    findings = compare(before, after, [Change("M", path, path)])
    assert len([f for f in findings if f.rule == rule]) == (0 if consumer_available else len(rows))


@pytest.mark.parametrize("rule", ["BLE001", "S110"])
@pytest.mark.parametrize("binding", [
    "initial", "after-other-import", "over-parameter", "after-call", "after-assignment",
    "after-pass", "after-string", "conditional", "after-return", "repeated", "rebound", "global", "decorated",
])
def test_documentation_does_not_change_import_dominance_or_identity(rule, binding):
    signature = "def dispatch(self, value, replay=None)" if binding == "over-parameter" else "def dispatch(self, value)"
    import_line = "        from pkg.callback_owner import replay\n"
    prefix = '        """Documented forwarding entry point."""\n'
    before = {
        "after-other-import": "        import contextvars\n",
        "after-call": "        self.prepare(value)\n",
        "after-assignment": "        unused = value\n",
        "after-pass": "        pass\n",
        "after-string": '        "Not the leading docstring."\n',
        "after-return": "        return replay(self.sample, value)\n",
        "global": "        global replay\n",
    }.get(binding, "")
    if binding == "conditional":
        import_line = "        if value:\n            from pkg.callback_owner import replay\n"
    elif binding == "repeated":
        import_line *= 2
    after = "        replay = replacement\n" if binding == "rebound" else ""
    decorator = "    @defer\n" if binding == "decorated" else ""
    wrapper = "class Handler:\n" + decorator + "    " + signature + ":\n" + prefix + before + import_line + after
    wrapper += "        return replay(self.sample, value)\n\n"
    source = wrapper + _EXTRACT_HELPER
    base = measured(_EXTRACT_BASE, hits=[(rule, 7)])
    head = measured(source, hits=[(rule, n.lineno) for n in ast.walk(ast.parse(source))
                                 if isinstance(n, ast.ExceptHandler)])
    callback = measured("def replay(callback, value):\n    return callback(value)\n", hits=[], path="pkg/callback_owner.py")
    findings = compare({base.path: base, callback.path: callback},
                       {head.path: head, callback.path: callback}, [Change("M", base.path, head.path)])
    assert len([f for f in findings if f.rule == rule]) == (0 if binding in {
        "initial", "after-other-import", "over-parameter"} else 1)


@pytest.mark.parametrize("rule", ["BLE001", "S110"])
@pytest.mark.parametrize("local_module", ["asyncio", "contextvars"])
def test_documented_consumer_keeps_initial_local_scheduler_imports(rule, local_module):
    source = _CALLBACK_MODULE.replace("import " + local_module + "\n", "", 1)
    source = source.replace("    captured =", '    """Documented consumer with an unconditional scheduler import."""\n'
                            + "    import " + local_module + "\n    captured =", 1)
    # Keep this wrapper undocumented to isolate the consumer's local-module proof.
    wrapper = """class Handler:
    async def dispatch(self, value):
        from pkg.callback_owner import run_owned as replay
        return await replay(self.context, self.sample, value, on_unowned=lambda: None)

"""
    head_source = wrapper + _EXTRACT_HELPER.replace("    def sample", "    async def sample")
    base = measured(_EXTRACT_BASE.replace("    def dispatch", "    async def dispatch"), hits=[(rule, 7)])
    head = measured(head_source, hits=[(rule, n.lineno) for n in ast.walk(ast.parse(head_source))
                                      if isinstance(n, ast.ExceptHandler)])
    callback = measured(source, hits=[], path="pkg/callback_owner.py")
    findings = compare({base.path: base, callback.path: callback},
                       {head.path: head, callback.path: callback}, [Change("M", base.path, head.path)])
    assert not [f for f in findings if f.rule == rule]
