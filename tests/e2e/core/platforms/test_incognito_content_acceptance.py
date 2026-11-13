"""Content-to-answer and backend acceptance through the real disposable gateway.

Only the model, Telegram Bot API and image-provider HTTP endpoints are stand-ins.
PDF extraction, tools, plugin discovery, kernels, private cache and upload are real.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import secrets
import zlib
from pathlib import Path

import pytest
from aiohttp import web
from PIL import Image

from tests.e2e.core.platforms import _contract as C
from tests.e2e.core.platforms._drv_telegram import TelegramDriver
from tests.e2e.core.platforms._suite import rig_fixtures
from tests.e2e.core.platforms.test_incognito_leak_scan import (
    _ask, _baseline, _hits, _interval_dirs, _leftovers, _reboot, _report, _slash,
)
from tests.fakes import recording_memory_provider
from tests.fakes.fake_llm_provider import Text, ToolCall
from tests.fakes.platforms._standin import StandinServer, wait_until

pytestmark = [pytest.mark.spawns_gateway_lookalike]
rig, rig_stream = rig_fixtures(TelegramDriver)


def _image_bytes(fmt="PNG", color=(37, 151, 209)):
    out = io.BytesIO()
    Image.new("RGB", (32, 32), color).save(out, format=fmt)
    return out.getvalue()


class ImageProviderHTTP(StandinServer):
    """The two OpenAI image-provider HTTP boundaries, using existing stand-in plumbing."""

    def __init__(self):
        super().__init__()
        self.image = _image_bytes(color=(211, 71, 31))

    def build_app(self):
        app = web.Application()
        app.router.add_post("/v1/images/generations", self.generate)
        app.router.add_get("/generated.png", self.download)
        return app

    async def generate(self, request):
        params = await request.json()
        response = {"created": 1, "data": [{"url": self.base_url + "/generated.png"}]}
        self.record("generate", params, response)
        return web.json_response(response)

    async def download(self, request):
        self.record("download", {}, {"size": len(self.image)})
        return web.Response(body=self.image, content_type="image/png")


def _pdf(text):
    """One-page PDF with a real text layer, compressed so raw-byte reads cannot pass."""
    stream = zlib.compress(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode())
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(stream)} /Filter /FlateDecode >>\nstream\n".encode() + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    data, offsets = b"%PDF-1.4\n", [0]
    for n, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += f"{n} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(data)
    data += f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode()
    data += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    return data + f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()


def _latest_image(body):
    """Require decodable pixel content, not an image-shaped placeholder or filename."""
    user = next((m for m in reversed(body.get("messages", [])) if m.get("role") == "user"), {})
    content = user.get("content")
    if not isinstance(content, list):
        return False
    for part in content:
        if part.get("type") != "image_url":
            continue
        url = part.get("image_url", {}).get("url", "")
        if not url.startswith("data:image/") or ";base64," not in url:
            continue
        try:
            image = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))).convert("RGB")
            # JPEG ingress/cache may change channel values slightly.
            pixel = image.getpixel((16, 16))
            if image.size == (32, 32) and all(abs(a - b) <= 4 for a, b in zip(pixel, (37, 151, 209))):
                return True
        except (ValueError, OSError):
            pass
    return False


def _tool_content(body):
    last = next((m for m in reversed(body.get("messages", [])) if m.get("role") == "tool"), {})
    content = last.get("content", "")
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content


def _conditional_answer(aid, predicate):
    return lambda body: C.answer(aid, "content verified") if predicate(body) else Text("Required content missing")


def _ledger(gw):
    path = gw.root / "memory-ledger.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _configure(rig, provider):
    import hermes_yaml as yaml

    home = rig.gw.hermes_home
    plugin = home / "plugins" / "incognito-recording"
    plugin.mkdir(parents=True)
    plugin.joinpath("__init__.py").write_text(Path(recording_memory_provider.__file__).read_text())
    path = home / "config.yaml"
    cfg = yaml.safe_load(path.read_text())
    cfg["memory"] = {"provider": "incognito-recording"}
    cfg["model"]["default"] = "fake-public-prior"
    cfg["incognito"]["model"] = "fake-private"
    cfg["agent"]["disabled_toolsets"] = []
    cfg["platform_toolsets"] = {"telegram": ["file", "image_gen", "code_execution"]}
    cfg["approvals"] = {"mode": "off"}
    # The generated delivery URL intentionally points to our loopback stand-in.
    cfg["security"] = {"allow_private_urls": True}
    cfg["image_gen"] = {"provider": "openai", "openai": {
        "base_url": provider.base_url + "/v1", "key_env": "IMAGE_ACCEPTANCE_TOKEN"}}
    path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    # Fake credential only; the configured provider endpoint is loopback.
    rig.gw._env["IMAGE_ACCEPTANCE_TOKEN"] = "sk-fake-image-acceptance"
    rig.gw._env["OPENAI_API_KEY"] = "sk-fake-image-acceptance"
    with (home / ".env").open("a") as out:
        out.write("IMAGE_ACCEPTANCE_TOKEN=sk-fake-image-acceptance\nOPENAI_API_KEY=sk-fake-image-acceptance\n")
    _reboot(rig)


def test_content_oracles_reject_missing_or_placeholder_payloads():
    aid = "oracle-control"
    answer = _conditional_answer(aid, _latest_image)
    for content in ("photo.jpg", [{"type": "image_url", "image_url": {"url": "photo.jpg"}}],
                    [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,eA=="}}]):
        assert C.foot(aid) not in answer({"messages": [{"role": "user", "content": content}]}).text
    data = base64.b64encode(_image_bytes("JPEG")).decode()
    assert C.foot(aid) in answer({"messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + data}}]}]}).text
    word = secrets.token_hex(24)
    pdf_answer = _conditional_answer(aid, lambda body: word in _tool_content(body))
    assert C.foot(aid) not in pdf_answer({"messages": [{"role": "user", "content": word}]}).text
    assert C.foot(aid) not in pdf_answer({"messages": [{"role": "tool", "content": "PDF extraction failed"}]}).text
    assert C.foot(aid) in pdf_answer({"messages": [{"role": "tool", "content": word}]}).text
    assert word.encode() not in _pdf(word)


def test_private_content_tools_memory_and_restored_request(rig):
    assert not _baseline(), "acceptance requires final-mode privacy assertions"
    word = "incognito-" + secrets.token_hex(24)
    tag = hashlib.sha256(word.encode()).hexdigest()[:10]
    public = "public-fact-" + secrets.token_hex(16)
    resumed = "resumed-fact-" + secrets.token_hex(16)
    drv, gw, director = rig.drv, rig.gw, rig.director
    parent = rig.private_parent
    assert parent is not None

    with ImageProviderHTTP() as provider:
        _configure(rig, provider)
        director.script("before-" + tag, C.answer("A-before-" + tag, "public stored"))
        _ask(rig, f"{public} [in:before-{tag}]", "A-before-" + tag)
        C.barrier(rig, "before-barrier-" + tag)
        wait_until(lambda: any(public in str(row) for row in _ledger(gw)), "normal provider write", on_timeout=rig.ctx)
        ledger_before = _ledger(gw)
        prior_model = director.requests["before-" + tag][0]["model"]
        assert prior_model == "fake-public-prior"

        _slash(rig, "/incognito on", "Incognito ON")
        photo_token, photo_aid = "photo-" + tag, "A-photo-" + tag
        director.script(photo_token, _conditional_answer(photo_aid, _latest_image))
        photo = drv.photo(_image_bytes("JPEG"), f"{word} describe the pixels [in:{photo_token}]")
        wait_until(lambda: director.requests.get(photo_token), "photo model request", on_timeout=rig.ctx)
        assert _latest_image(director.requests[photo_token][0]), "actual photo pixels never reached the model"
        C.wait_reply(rig, photo.chat_id, photo_aid)
        C.barrier(rig, "photo-barrier-" + tag)
        assert director.requests[photo_token][0]["model"] == "fake-private"

        pdf_word = word + "-pdf-text"
        pdf_token, pdf_aid = "pdf-" + tag, "A-pdf-" + tag

        def read_pdf(body):
            user = next(m for m in reversed(body["messages"]) if m.get("role") == "user")
            paths = re.findall(r"(/[^\s\"'<>]+\.pdf)", str(user["content"]))
            return ToolCall("read_file", {"path": paths[-1]}) if paths else Text("PDF path missing")

        director.script(pdf_token, read_pdf,
                        _conditional_answer(pdf_aid, lambda body: pdf_word in _tool_content(body)))
        pdf = drv.document("content.pdf", _pdf(pdf_word), "application/pdf", f"Read the text [in:{pdf_token}]")
        wait_until(lambda: len(director.requests.get(pdf_token, [])) >= 2, "real PDF extraction tool result", on_timeout=rig.ctx)
        assert pdf_word not in str(director.requests[pdf_token][0]), "PDF content leaked into caption instead of extraction"
        assert pdf_word in _tool_content(director.requests[pdf_token][1]), "PDF extraction did not reach the next model request"
        C.wait_reply(rig, pdf.chat_id, pdf_aid)
        C.barrier(rig, "pdf-barrier-" + tag)

        image_token, image_aid = "generate-" + tag, "A-generate-" + tag
        generated = []

        def deliver_image(body):
            try:
                payload = json.loads(_tool_content(body))
            except ValueError:
                return Text("Generated result was not JSON")
            path = Path(payload.get("image", ""))
            if not payload.get("success") or not path.is_file():
                return Text("Generated file missing")
            generated.append(path)
            return C.answer(image_aid, f"\nMEDIA:{path}\n")

        director.script(image_token, ToolCall("tool_call", {"calls": [
            {"name": "image_generate", "arguments": {"prompt": word}}]}), deliver_image)
        uploads_before = len(drv.standin.calls_of("sendPhoto"))
        image_in = drv.dm(f"Generate an image [in:{image_token}]")
        wait_until(lambda: len(director.requests.get(image_token, [])) >= 2, "image tool result", on_timeout=rig.ctx)
        assert generated, f"image_generate did not return a generated file: {_tool_content(director.requests[image_token][1])}"
        C.wait_reply(rig, image_in.chat_id, image_aid)
        C.barrier(rig, "image-barrier-" + tag)
        assert len(provider.calls_of("generate")) == 1
        assert provider.calls_of("generate")[0].params["prompt"] == word
        assert provider.calls_of("download"), "image provider URL was never downloaded"
        assert generated and generated[0].is_relative_to(parent)
        assert generated[0].read_bytes() == provider.image
        wait_until(lambda: len(drv.standin.calls_of("sendPhoto")) > uploads_before,
                   "generated image upload", on_timeout=rig.ctx)
        sends = drv.standin.calls_of("sendPhoto")[uploads_before:]
        assert any(not call.faulted and call.params.get("photo", {}).get("sha256") == hashlib.sha256(provider.image).hexdigest()
                   for call in sends), f"generated bytes never reached the stand-in chat upload: {sends}"

        assert _ledger(gw) == ledger_before, "private turns wrote backend records"

        aux_before = len([r for r in rig.llm.requests if not r.get("body", {}).get("tools")])
        _slash(rig, "/compress", "Compressed:")
        # A real summary request, not a no-op /compress acknowledgement.
        aux_after = [r for r in rig.llm.requests if not r.get("body", {}).get("tools")]
        assert len(aux_after) > aux_before
        assert any(word in str(r) for r in aux_after[aux_before:]), "compaction did not consume private context"
        assert _ledger(gw) == ledger_before, "private compaction wrote backend records"
        assert _interval_dirs(parent), "no private interval directory existed"

        _slash(rig, "/incognito off", "Incognito OFF")
        assert _ledger(gw) == ledger_before, "leaving incognito flushed private backend records"
        assert not generated[0].exists(), "generated image survived /incognito off"
        assert not _leftovers(parent)
        assert not _hits(gw, parent, word.encode())

        post_token, post_aid = "after-" + tag, "A-after-" + tag
        director.script(post_token, C.answer(post_aid, "normal again"))
        _ask(rig, f"{resumed} [in:{post_token}]", post_aid)
        C.barrier(rig, "after-barrier-" + tag)
        post = director.requests[post_token][0]
        assert public in str(post), "pre-incognito fact was not restored"
        assert word not in str(post), "private context reached the restored normal model"
        assert post["model"] == prior_model, "actual prior model was not restored"
        wait_until(lambda: any(resumed in str(row) for row in _ledger(gw)), "normal provider writes resume", on_timeout=rig.ctx)
        assert word not in json.dumps(_ledger(gw))
        assert not _hits(gw, parent, word.encode())
        _report("content", {"photo_pixels_in_request": True, "pdf_extracted_before_answer": True,
                            "generated_image_uploaded_and_removed": True,
                            "memory_write_control_and_resume": True, "private_backend_writes": 0,
                            "prior_model": prior_model, "post_off_model": post["model"],
                            "public_fact_restored": True, "private_fact_absent": True})
