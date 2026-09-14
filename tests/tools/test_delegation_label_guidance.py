"""The actual model-facing authoring paths expose the explicit new-label bound."""
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
        assert 'Maximum 24 Unicode code points in task_label itself' in description
        assert 'role suffixes do not count' in description
        assert 'hard admission limit, not truncation' in description
        assert 'never use the goal' in schema['description']
        assert schema['maxLength'] == 24
    role = props['tasks']['items']['properties'].get('subagent_type')
    if role is not None:  # only advertised when named roles are configured
        assert '24 characters' not in role['description']
