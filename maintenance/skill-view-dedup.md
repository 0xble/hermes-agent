# Skill view dedup

- **Patch identity:** `skill-view-dedupe`
- **Contract tests:** `tests/tools/test_skill_view_dedup.py`
- **Behavior:** A repeat `skill_view` of the same skill file in the same task returns a short pointer instead of the full body when the earlier view is still in context. The digest covers the body plus the stable fields the model acts on (linked files, dependency status, trust, tier), so any change to those returns the full view. `mark_context_loss_boundary` (compaction and committed pruning) forgets the task's served views, so the next view reloads in full. SKILL.md and each linked file are tracked separately, setup-needed views are never deduplicated, and tracked tasks and per-task entries are both bounded.
- **Surfaces:** `tools/skills_tool.py`, `tools/skills_tool_dedup.py`, and `agent/conversation_compression_boundary.py`.
- **Retirement:** Remove this patch and its focused tests when upstream deduplicates repeated skill views within a context generation.
- **Rollback:** Revert the commits carrying `Fork-Patch: skill-view-dedupe`.
