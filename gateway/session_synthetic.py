"""Private codec provenance for gateway-authored prompts, never an inbound metadata grant."""
from copy import deepcopy

_TOKEN = object()


def mark_synthetic(event):
    # Only producer entry points mint this capability. JSON metadata cannot supply it.
    event._native_synthetic = _TOKEN


def is_synthetic(event):
    return getattr(event, '_native_synthetic', None) is _TOKEN


def project_synthetic(event):
    """Keep producer routing/fences outside the ordinary adapter metadata contract."""
    if not is_synthetic(event):
        return None
    return {'internal': event.internal, 'metadata': deepcopy(event.metadata)}


def restore_synthetic(event, descriptor):
    event.internal = descriptor['internal']
    event.metadata = deepcopy(descriptor['metadata'])
    mark_synthetic(event)
