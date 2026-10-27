"""In-process admission refusal presentation, bounded per chat and silent for producers."""
import logging
from time import monotonic

from gateway.platforms.event import turn_origin_for_event
from gateway.session_synthetic import is_synthetic

logger = logging.getLogger(__name__)
WINDOW_SECONDS = 600


def internal_input(event):
    return (is_synthetic(event) or bool(event.internal)
            or (isinstance(event.metadata, dict)
                and turn_origin_for_event(event) in {'goal_loop', 'cron', 'process'}))


def _chat_key(source):
    return (getattr(getattr(source, 'platform', None), 'value', None),
            getattr(source, 'chat_id', None))


def log_internal_refusal(owner, source, reason, clause=None):
    """One operator warning per owner/window, later attempts only update/log counts."""
    now = monotonic()
    previous = getattr(owner, '_internal_refusal_window', None)
    fresh = previous is None or now - previous[0] >= WINDOW_SECONDS
    window = previous if previous is not None and not fresh else (now, {})
    key = f'{reason}:{clause or "unspecified"}'
    window[1][key] = window[1].get(key, 0) + 1
    owner._internal_refusal_window = window
    logger.log(logging.WARNING if fresh else logging.DEBUG,
               'Internal admission refused: %s (%s) platform=%s chat=%s counts=%s previous_window=%s',
               reason, clause, *_chat_key(source), dict(window[1]),
               dict(previous[1]) if fresh and previous else {})


def user_notice_due(authority, event, reason):
    """A chat gets at most one notice for a reason in each 10-minute window."""
    now = monotonic()
    windows = getattr(authority, '_refusal_notice_windows', {})
    authority._refusal_notice_windows = windows
    key = (*_chat_key(event.source), reason)
    window = windows.get(key)
    if window is not None and now - window[0] < WINDOW_SECONDS:
        window[1] += 1
        logger.info('Admission refusal notice suppressed: reason=%s platform=%s chat=%s suppressed=%s',
                    reason, *key[:2], window[1])
        return False
    for expired in [k for k, value in windows.items() if now - value[0] >= WINDOW_SECONDS]:
        del windows[expired]
    windows[key] = [now, 0]
    return True
