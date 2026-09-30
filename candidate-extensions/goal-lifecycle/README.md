# Goal lifecycle candidate plugin

This plugin is the Slice 3 candidate extension. It gives the model one
restricted tool, `goal_set`, for automatic enrollment and additive subgoals.
User controls remain `/goal pause`, `/goal resume`, and `/goal clear`.

Install it only into a disposable candidate profile:

```sh
mkdir -p "$HERMES_HOME/plugins/goal-lifecycle"
cp plugin.yaml __init__.py "$HERMES_HOME/plugins/goal-lifecycle/"
hermes plugins doctor "$HERMES_HOME/plugins/goal-lifecycle" --ci
```

The plugin requires a trusted `task_id` or `session_id` supplied by Hermes
runtime context. It never accepts session scope from model arguments.
