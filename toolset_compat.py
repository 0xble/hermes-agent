"""Lightweight compatibility constants shared by toolset resolvers.

Keep legacy aliases here so diagnostic and runtime paths cannot drift while
avoiding the discovery side effects of importing ``model_tools``.
"""

_LEGACY_TOOLSET_MAP = {
    "web_tools": ["web_search", "web_extract"],
    "terminal_tools": ["terminal"],
    "vision_tools": ["vision_analyze"],
    "image_tools": ["image_generate"],
    "skills_tools": ["skills_list", "skill_view", "skill_manage"],
    "cronjob_tools": ["cronjob_manage"],
    "file_tools": ["read_file", "write_file", "patch", "search_files"],
    "tts_tools": ["text_to_speech"],
}
