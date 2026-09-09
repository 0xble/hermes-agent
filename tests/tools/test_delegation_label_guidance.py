"""The actual model-facing authoring paths expose guidance, not a new hard bound."""
from tools import delegate_tool  # noqa: F401 — register the real schema
from tools.registry import registry


def test_model_facing_task_labels_have_24_character_guidance_on_both_paths():
    definitions = registry.get_definitions({'delegate_task'}, quiet=True)
    props = definitions[0]['function']['parameters']['properties']
    for schema in (props['task_label'], props['tasks']['items']['properties']['task_label']):
        assert 'Use a short, imperative display label of at most 24 characters, including spaces.' in schema['description']
        assert 'never use the goal' in schema['description']
        assert 'maxLength' not in schema
    role = props['tasks']['items']['properties'].get('subagent_type')
    if role is not None:  # only advertised when named roles are configured
        assert '24 characters' not in role['description']
