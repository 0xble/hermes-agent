"""Slack system-prompt platform hint (``PlatformEntry.platform_hint``), kept out of the adapter facade.

These bytes land in every cached system prompt for this platform: edit deliberately.
"""

PLATFORM_HINT = (
    'You are in a Slack workspace communicating with your user. Standard markdown is auto-converted to Slack '
    'formatting (bold, headers, links, code). Tables fall back to monospace, so prefer bullets or labeled '
    'lines. You can send media files natively: include MEDIA:/absolute/path/to/file in your response. Images '
    '(.png, .jpg, .webp) are uploaded as photo attachments, audio as file attachments. You can also include '
    'image URLs in markdown format ![alt](url) and they will be uploaded as attachments.'
)

RICH_BLOCKS_HINT = (
    "You are in Slack. Markdown renders native headers, lists, quotes, code and aligned pipe tables. "
    "Use tables for comparisons, standalone ![alt](https://url) for a full image, "
    "and ![thumb:alt](https://url) as a paragraph's final line for a right thumbnail. "
    "Use -# text for context/source notes, optionally -# ![alt](https://url) text for an icon. "
    "For peer cards use a fenced cards YAML list: - title: Name, with optional indented subtitle, body, "
    "image (HTTPS URL), url (HTTPS title link, not a button). Maximum 10 cards, title/subtitle 150 characters, body 200. "
    "Keep YAML fields on separate lines. Native images are not also uploaded. Other markdown image URLs "
    "remain attachments. MEDIA:/absolute/path sends files. Plain-text fallback is always retained."
)
