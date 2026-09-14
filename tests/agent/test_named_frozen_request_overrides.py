"""Configured authority survives real child construction and SDK dispatch, offline."""
from copy import deepcopy
import json

import httpx
import openai
import pytest

from tests.agent.test_run_agent_codex_responses import _patch_agent_bootstrap


@pytest.mark.parametrize('channel,key', [
    ('extra_headers', 'X-Access-Token'), ('extra_query', 'access_token'),
    ('extra_body', 'credentials'),
])
@pytest.mark.parametrize('mutation', ['unchanged', 'replace', 'remove'])
def test_configured_override_survives_child_and_final_sdk_boundary(monkeypatch, tmp_path, channel, key, mutation):
    from run_agent import AIAgent
    from tools import delegate_tool
    from agent.chat_completion_helpers import _dispatch_nonstreaming_api_request

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request',
                        lambda *a, **kw: pytest.fail('unexpected real HTTP transport'))
    monkeypatch.setattr('agent.context_compressor.get_model_context_length', lambda *a, **kw: 128000)
    _patch_agent_bootstrap(monkeypatch)
    value = {'access_token': 'synthetic-frozen'} if channel == 'extra_body' else 'synthetic-frozen'
    overrides = {channel: {key: value}}
    cfg = {'model': 'gpt-4o', 'base_url': 'https://fixture.invalid/v1',
           'api_key': 'synthetic-main-key', 'api_mode': 'chat_completions',
           'request_overrides': overrides,
           'subagents': {'worker': {'description': 'Fixture role', 'instructions': 'Return fixture evidence.'}}}
    parent = AIAgent(model=cfg['model'], provider='custom', base_url=cfg['base_url'],
                     api_key=cfg['api_key'], api_mode=cfg['api_mode'], quiet_mode=True,
                     skip_context_files=True, skip_memory=True, skip_background_review=True)
    child = None
    requests = []
    def response(request):
        requests.append(request)
        return httpx.Response(200, json={'id': 'chat-fixture', 'object': 'chat.completion',
            'created': 0, 'model': cfg['model'], 'choices': []})
    client = openai.OpenAI(api_key=cfg['api_key'], base_url=cfg['base_url'],
        default_headers={'X-Fixture-SDK-Default': 'allowed-default'},
        http_client=httpx.Client(transport=httpx.MockTransport(response)))
    try:
        launches, error = delegate_tool._preflight_task_runtime(
            [{'goal': 'Return fixture evidence.', 'subagent_type': 'worker'}], cfg, None, parent, None)
        assert error is None
        launch = launches[0]
        child = delegate_tool._build_child_agent(
            task_index=0, goal='Return fixture evidence.', context=None, toolsets=[],
            model=launch.credentials['model'], max_iterations=1, task_count=1, parent_agent=parent,
            **delegate_tool._creds_overrides(launch.credentials),
            subagent_definition=launch.definition, resolved_reasoning=launch.reasoning)
        assert child._delegation_runtime_pin is not None
        frozen = deepcopy(child.request_overrides)
        kwargs = child._build_api_kwargs([{'role': 'user', 'content': 'fixture'}], tools_for_api=[])
        assert kwargs[channel][key] == value
        # Mutate only final kwargs, as middleware can, leaving launch authority intact.
        kwargs = deepcopy(kwargs)
        if mutation == 'replace':
            kwargs[channel][key] = {'access_token': 'synthetic-foreign'} if channel == 'extra_body' else 'synthetic-foreign'
        elif mutation == 'remove':
            del kwargs[channel][key]
        else:
            if channel == 'extra_headers':
                kwargs[channel][key.lower()] = kwargs[channel].pop(key)
            kwargs.setdefault('extra_headers', {})['X-Fixture-Trace'] = 'allowed-default'
            kwargs.setdefault('extra_body', {})['fixture_default'] = True
        if mutation == 'unchanged':
            _dispatch_nonstreaming_api_request(child, kwargs, make_client=lambda *a, **kw: client)
            assert len(requests) == 1
            assert requests[0].headers['X-Fixture-SDK-Default'] == 'allowed-default'
            actual = (requests[0].headers[key] if channel == 'extra_headers' else
                      requests[0].url.params[key] if channel == 'extra_query' else json.loads(requests[0].content)[key])
            assert actual == value
        else:
            with pytest.raises(ValueError, match='pinned request overrides changed'):
                _dispatch_nonstreaming_api_request(child, kwargs, make_client=lambda *a, **kw: client)
            assert requests == []
        assert child.request_overrides == frozen
    finally:
        client.close()
        if child is not None:
            child.close()
        parent.close()
