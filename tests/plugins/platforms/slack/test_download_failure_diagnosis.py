"""2026-10-24: a HEIC the bot downloaded fine was reported as "Slack blocked the file download"."""

from plugins.platforms.slack.adapter import SlackAdapter

FILE = {"name": "Screenshot.heic"}


def _describe(message):
    adapter = SlackAdapter.__new__(SlackAdapter)
    return adapter._describe_slack_download_failure(ValueError(message), file_obj=FILE)


def test_login_page_is_an_access_problem():
    detail = _describe("Refusing to cache non-image data as .jpg (starts with: '<!DOCTYPE html><html>')")
    assert "scope, auth, or file-permission" in detail


def test_undecodable_image_is_not_blamed_on_slack():
    detail = _describe("Refusing to cache non-image data as .heic (starts with: '\\x00\\x00\\x00$ftypheic')")
    assert "unsupported format" in detail and "permission" not in detail


def test_svg_is_unsupported_not_an_access_problem():
    detail = _describe("Refusing to cache non-image data as .png (starts with: '<svg xmlns=\"http://www.w3.org\"')")
    assert "unsupported format" in detail


def test_login_page_after_blank_lines_is_still_an_access_problem():
    detail = _describe("Refusing to cache non-image data as .jpg (starts with: '\\r\\n\\r\\n<!DOCTYPE html>')")
    assert "scope, auth, or file-permission" in detail
