"""Turn-local evidence for main-agent fallback connection domains.

Selection stays in the existing monotonic chain walker. These helpers only hold
proven connection failures, requested routes and the one bounded cap allowance.
"""
from __future__ import annotations

from agent.backend_identity import BackendIdentity, FailureScope, should_skip_candidate


def active_route_identity(agent) -> BackendIdentity:
    return BackendIdentity.build(
        provider=getattr(agent, 'provider', ''), model=getattr(agent, 'model', ''),
        base_url=str(getattr(agent, 'base_url', '') or ''))


class IndependentAttemptExhausted(RuntimeError):
    """The switch-cap exception grants one request, not another retry loop."""


def note_route_request(agent) -> None:
    """Record execution, never activation, and retain authored labels for copy."""
    if getattr(agent, '_turn_independent_attempt_active', False) is True:
        if getattr(agent, '_turn_independent_attempt_requested', False) is True:
            raise IndependentAttemptExhausted('Independent fallback request allowance exhausted')
        agent._turn_independent_attempt_requested = True
    identity = active_route_identity(agent)
    routes = getattr(agent, '_turn_attempted_routes', None)
    if not isinstance(routes, list):
        routes = agent._turn_attempted_routes = []
    if identity not in routes:
        routes.append(identity)
        labels = getattr(agent, '_turn_attempted_route_labels', None)
        if not isinstance(labels, list):
            labels = agent._turn_attempted_route_labels = []
        labels.append(f'{agent.model} via {agent.provider}')


def note_endpoint_failure(agent, classified) -> None:
    if getattr(classified, 'failure_scope', None) != FailureScope.ENDPOINT:
        return
    identity = active_route_identity(agent).connection_domain()
    if not identity.base_url:
        return
    failed = getattr(agent, '_turn_failed_endpoints', None)
    if not isinstance(failed, list):
        failed = agent._turn_failed_endpoints = []
    if identity not in failed:
        failed.append(identity)


def failed_connection_domain(agent, candidate: BackendIdentity) -> bool:
    failed = getattr(agent, '_turn_failed_endpoints', None)
    return isinstance(failed, list) and any(
        should_skip_candidate(candidate.connection_domain(), route.connection_domain(), FailureScope.ENDPOINT)
        for route in failed)


def reserve_independent_attempt(agent) -> bool:
    """One request at the switch cap, only on a provably unvisited socket.

    The global restart counter is not increased or reset. Recording requests at
    the execution boundary excludes aliases merely bound before hitting the cap.
    """
    if getattr(agent, '_turn_independent_attempt_used', False) is True:
        return False
    failed = getattr(agent, '_turn_failed_endpoints', None)
    visited = getattr(agent, '_turn_attempted_routes', None)
    if not isinstance(failed, list) or not failed or not isinstance(visited, list) or not visited:
        return False
    candidate = active_route_identity(agent).connection_domain()
    if not candidate.base_url or failed_connection_domain(agent, candidate):
        return False
    if any(not route.connection_domain().base_url or should_skip_candidate(
            candidate, route.connection_domain(), FailureScope.ENDPOINT) for route in visited):
        return False
    agent._turn_independent_attempt_used = True
    agent._turn_independent_attempt_active = True
    return True


def attempted_routes_summary(agent) -> str:
    from agent.i18n import t
    labels = getattr(agent, '_turn_attempted_route_labels', None)
    if isinstance(labels, list) and labels:
        return ', '.join(labels)
    routes = getattr(agent, '_turn_attempted_routes', None)
    if isinstance(routes, list) and routes:
        return ', '.join(f'{r.model} via {r.provider}' for r in routes)
    return t('explainer.shared.no_routes_requested')


def append_attempt_summary(agent, text: str) -> str:
    from agent.i18n import t
    routes = getattr(agent, '_turn_attempted_routes', None)
    if not isinstance(routes, list) or not routes:
        return text
    if getattr(agent, '_turn_fallback_chain_exhausted', False) is True:
        text += '\n\n' + t('explainer.shared.chain_exhausted')
    return text + '\n\n' + t('explainer.shared.attempted_routes', routes=attempted_routes_summary(agent))