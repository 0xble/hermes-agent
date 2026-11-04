"""A dead shared socket is one failure domain, not four provider attempts.

Only loopback fake servers are used. The primary and chain entries intentionally
inherit endpoints from provider definitions, as in the overnight proxy outage.
"""
from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import errno
import json
import socket
from threading import Thread
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import openai
import pytest

from agent.backend_identity import BackendIdentity, FailureScope, same_endpoint
from agent.chat_completion_helpers import _should_skip_fallback_candidate
from agent.error_classifier import classify_api_error
from agent.turn_iteration_prep import apply_retry_restarts
from agent.turn_retry_state import TurnRetryState


@contextmanager
def _server():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if not self.path.endswith('/chat/completions'):
                self.send_error(404)
                return
            requests.append(body)
            data = json.dumps({
                'id': 'local', 'object': 'chat.completion', 'created': 1,
                'model': body['model'],
                'choices': [{'index': 0, 'finish_reason': 'stop',
                             'message': {'role': 'assistant', 'content': 'Grok actually answered.'}}],
                'usage': {'prompt_tokens': 1, 'completion_tokens': 4, 'total_tokens': 5},
            }).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1', requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def routes(monkeypatch):
    # Pick a free loopback port, then release it: Darwin silently drops SYNs to a
    # still-bound non-listening socket (a timeout, not the refusal under test).
    with socket.socket() as refused, _server() as (healthy, requests):
        refused.bind(('127.0.0.1', 0))
        dead = f'http://127.0.0.1:{refused.getsockname()[1]}/v1'
        refused.close()
        config = {'providers': {
            'sol': {'base_url': dead}, 'claude': {'base_url': dead},
            'muse': {'base_url': dead},
        }, 'model': {'context_length': 256000}, 'agent': {'auto_recover': {'enabled': False}}}
        monkeypatch.setattr('hermes_cli.config.load_config', lambda: config)
        monkeypatch.setattr('hermes_cli.config.load_config_readonly', lambda: config)
        monkeypatch.setenv('XAI_BASE_URL', healthy)
        yield dead, healthy, config, requests


def _agent(routes):
    from run_agent import AIAgent
    dead, healthy, config, requests = routes
    chain = [{'provider': p, 'model': m} for p, m in [
        ('custom:claude', 'Claude'), ('custom:muse', 'Muse'), ('custom:sol', 'Sol'), ('xai-oauth', 'Grok')]]
    with patch('model_tools.get_tool_definitions', return_value=[]), \
            patch('model_tools.check_toolset_requirements', return_value={}):
        agent = AIAgent(provider='custom:sol', model='Sol', api_key='local-test-only', base_url=dead,
                        quiet_mode=True, skip_context_files=True, skip_memory=True,
                        fallback_model=chain)
    agent.client = openai.OpenAI(api_key='local-test-only', base_url=dead, max_retries=0,
                                timeout=httpx.Timeout(1))
    agent._cached_system_prompt = 'Answer the user.'
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent._disable_streaming = True
    agent._api_max_retries = 3
    return agent


@contextmanager
def _run_locally(agent, routes):
    # Only OAuth credential/client construction is replaced for Grok, not activation,
    # request execution, retry routing, rebuilt-message handling, or response processing.
    from agent.auxiliary_client import resolve_provider_client
    _, healthy, _, _ = routes
    activated = []

    def local_client(provider, **kwargs):
        activated.append(provider)
        if provider == 'xai-oauth':
            return openai.OpenAI(api_key='local-test-only', base_url=healthy,
                                 max_retries=0, timeout=1), kwargs['model']
        assert provider.startswith('custom:'), 'never resolve a live vendor in this test'
        client, model = resolve_provider_client(provider, **kwargs)
        client.max_retries = 0
        client.timeout = httpx.Timeout(1)
        return client, model

    with patch('agent.auxiliary_client.resolve_provider_client', side_effect=local_client), \
            patch('agent.turn_api_error.interruptible_backoff_sleep', return_value=None), \
            patch.object(agent, '_try_recover_primary_transport', return_value=False), \
            patch.object(agent, '_persist_session'), patch.object(agent, '_save_trajectory'), \
            patch.object(agent, '_cleanup_task_resources'):
        yield activated


def test_refused_inherited_proxy_skips_aliases_and_grok_executes(routes):
    agent = _agent(routes)
    with _run_locally(agent, routes) as activated:
        result = agent.run_conversation('Please answer.')
    assert result['final_response'] == 'Grok actually answered.'
    assert result['turn_exit_reason'].startswith('text_response')
    assert activated == ['xai-oauth'], 'dead sibling aliases must not even construct clients'
    assert [r['model'] for r in routes[3]] == ['Grok']


def _identity(url, provider='sol', model='Sol'):
    return BackendIdentity.build(provider=provider, base_url=url, model=model)


def test_offline_inherited_identity_never_reads_credentials_or_activates(routes):
    from agent.route_binding import resolve_route_identity
    dead, _, config, _ = routes
    config['providers']['claude']['key_env'] = 'MUST_NOT_READ'
    with patch('agent.secret_scope.get_secret_str', side_effect=AssertionError('credential read')), \
            patch('hermes_cli.runtime_provider_custom.get_secret_str', side_effect=AssertionError('credential read')), \
            patch('agent.auxiliary_client.resolve_provider_client', side_effect=AssertionError('activation')):
        identity = resolve_route_identity({'provider': 'custom:claude', 'model': 'Claude'})
    assert same_endpoint(identity, _identity(dead))


@pytest.mark.parametrize('kind', ['refused', 'dns', 'unreachable', 'connect_timeout'])
def test_proven_connection_failure_is_endpoint_scoped(kind):
    causes = {
        'refused': ConnectionRefusedError(errno.ECONNREFUSED, 'Connection refused'),
        'dns': socket.gaierror(socket.EAI_NONAME, 'nodename nor servname provided'),
        'unreachable': OSError(errno.ENETUNREACH, 'Network is unreachable'),
        'connect_timeout': httpx.ConnectTimeout('connect timed out'),
    }
    error = openai.APIConnectionError(request=httpx.Request('POST', 'http://127.0.0.1:1/v1'))
    error.__cause__ = causes[kind]
    classified = classify_api_error(error)
    assert classified.failure_scope == FailureScope.ENDPOINT


@pytest.mark.parametrize('error', [httpx.ReadTimeout('read timed out'),
                                   RuntimeError('Connection error.'),
                                   RuntimeError('429 rate limit'),
                                   RuntimeError('401 authentication failed'),
                                   RuntimeError('404 model not found')])
def test_other_failures_do_not_invalidate_endpoint(error):
    assert getattr(classify_api_error(error), 'failure_scope', FailureScope.MODEL) != FailureScope.ENDPOINT


def _skip(agent, entry):
    return _should_skip_fallback_candidate(
        agent, entry, (entry["provider"], entry["model"]), entry["provider"], entry["model"], set())


def _candidate_agent(dead):
    return SimpleNamespace(provider='sol', base_url=dead, api_mode='chat_completions', model='Sol',
                           _turn_failed_endpoints=[_identity(dead)],
                           _turn_attempted_routes=[_identity(dead)])


def test_inherited_alias_is_skipped_but_explicit_override_and_different_port_are_not(routes):
    dead, healthy, _, _ = routes
    agent = _candidate_agent(dead)
    assert _skip(agent, {'provider': 'custom:claude', 'model': 'Claude'})
    assert not _skip(agent, {'provider': 'custom:claude', 'model': 'Claude', 'base_url': healthy})
    # Same hostname, different port is a different failure domain.
    assert same_endpoint(_identity(dead), _identity(healthy)) is False


def test_connection_failure_covers_other_paths_and_wire_modes_on_same_socket(routes):
    dead, _, _, _ = routes
    agent = _candidate_agent(dead)
    assert _skip(agent, {
        'provider': 'custom:claude', 'model': 'Claude', 'base_url': dead + '/messages',
        'api_mode': 'anthropic_messages',
    })


def test_read_timeout_does_not_skip_sibling(routes):
    dead, _, _, _ = routes
    agent = _candidate_agent(dead)
    agent._turn_failed_endpoints = []
    assert not _skip(agent, {'provider': 'custom:claude', 'model': 'Claude'})


def _restart(agent, count):
    retry = TurnRetryState()
    retry.restart_with_rebuilt_messages = True
    return apply_retry_restarts(
        agent, _retry=retry, response=None, interrupted=False, messages=[], conversation_history=[],
        user_message='hi', api_kwargs={}, current_turn_user_idx=0, final_response=None,
        retry_count=0, max_retries=3, api_call_count=1, restart_count=count,
        length_continue_retries=0, _preflight_compression_blocked=True, _turn_exit_reason='unknown')


def test_at_cap_one_unvisited_independent_domain_gets_request_not_just_activation(routes):
    dead, healthy, _, requests = routes
    agent = _agent(routes)
    agent._turn_failed_endpoints = [_identity(dead)]
    agent._turn_attempted_routes = [_identity(dead)]
    with _run_locally(agent, routes):
        assert agent._try_activate_fallback()
        verdict = _restart(agent, 3)
        assert verdict.action == 'continue'
        # Exercise exactly the route admitted by the production restart gate.
        result = agent.client.chat.completions.create(model=agent.model, messages=[{'role': 'user', 'content': 'hi'}])
    assert result.choices[0].message.content == 'Grok actually answered.'
    assert [r['model'] for r in requests] == ['Grok']
    assert verdict.restart_count == 3, 'do not raise the global restart cap'
    assert _restart(agent, 3).action == 'break', 'one turn-local allowance only'


@pytest.mark.parametrize('visited, failed', [(True, True), (False, False)])
def test_cap_never_grants_visited_domain_or_non_connection_failure(routes, visited, failed):
    dead, healthy, _, _ = routes
    agent = _candidate_agent(dead)
    agent.provider, agent.model, agent.base_url = 'xai-oauth', 'Grok', healthy
    if visited:
        agent._turn_attempted_routes.append(_identity(healthy, 'xai-oauth', 'Grok'))
    if not failed:
        agent._turn_failed_endpoints = []
    agent.iteration_budget = SimpleNamespace(refund=MagicMock())
    assert _restart(agent, 3).action == 'break'
    agent.iteration_budget.refund.assert_not_called()


def test_next_turn_clears_failed_domain_and_primary_can_recover(routes):
    agent = _agent(routes)
    with _run_locally(agent, routes):
        first = agent.run_conversation('first')
        assert first['final_response'] == 'Grok actually answered.'
        # Return the primary to a working socket between turns; the turn-local failure
        # must not blacklist even the same provider or preserve the old attempt ledger.
        agent._primary_runtime['base_url'] = routes[1]
        agent._primary_runtime['client'] = openai.OpenAI(api_key='local-test-only', base_url=routes[1], max_retries=0)
        second = agent.run_conversation('second')
    assert second['final_response'] == 'Grok actually answered.'
    assert agent.provider == 'custom:sol'
    assert [r['model'] for r in routes[3]] == ['Grok', 'Sol']
    assert getattr(agent, '_turn_failed_endpoints', []) == []
    assert [i.model for i in agent._turn_attempted_routes] == ['sol']


def test_switch_limit_copy_names_only_requested_routes(routes):
    from agent.turn_finalizer import _explain_abnormal_exit
    import logging
    agent = _agent(routes)
    agent._turn_attempted_routes = [_identity(routes[0])]
    agent.provider, agent.model = 'xai-oauth', 'Grok'  # activated, never requested
    text = _explain_abnormal_exit(agent, None, 'rebuilt_restart_limit_exceeded', False, logging.getLogger(__name__))
    assert 'switch limit' in text.lower()
    assert 'sol' in text.lower()
    assert 'Grok' not in text
    assert 'every route' not in text.lower()


def test_full_turn_at_switch_cap_still_requests_the_independent_route(routes):
    from functools import wraps
    agent = _agent(routes)

    @wraps(apply_retry_restarts)
    def at_cap(agent, **kwargs):
        if agent.model == 'Grok' and kwargs['_retry'].restart_with_rebuilt_messages:
            kwargs['restart_count'] = 3
        return apply_retry_restarts(agent, **kwargs)

    with _run_locally(agent, routes) as activated, \
            patch('agent.conversation_loop.apply_retry_restarts', new=at_cap):
        result = agent.run_conversation('answer at the cap')
    assert result['final_response'] == 'Grok actually answered.'
    assert activated == ['xai-oauth']
    assert [r['model'] for r in routes[3]] == ['Grok']
