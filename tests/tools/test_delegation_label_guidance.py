"""The actual model-facing authoring paths expose guidance, not a new hard bound."""
from tools import delegate_tool  # noqa: F401 — register the real schema
from tools.registry import registry


def test_model_facing_task_labels_have_adaptive_task_row_guidance_on_both_paths():
    definitions = registry.get_definitions({'delegate_task'}, quiet=True)
    props = definitions[0]['function']['parameters']['properties']
    for schema in (props['task_label'], props['tasks']['items']['properties']['task_label']):
        description = schema['description']
        assert 'sentence-case' in description
        assert 'Check API routing' in description
        assert 'not Review Context Forks' in description
        assert '24-character total task-card row' in description
        assert 'four spaces per nesting level, hierarchical reference' in description
        assert 'display guidance, not a hard limit' in description
        assert 'never use the goal' in schema['description']
        assert 'maxLength' not in schema
    role = props['tasks']['items']['properties'].get('subagent_type')
    if role is not None:  # only advertised when named roles are configured
        assert '24 characters' not in role['description']
