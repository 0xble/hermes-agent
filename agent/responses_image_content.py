"""Materialize completed Responses images through the existing media cache."""

from __future__ import annotations

import base64
import binascii
from io import BytesIO
import logging
from typing import Any

logger = logging.getLogger(__name__)


def generated_image_content(item: Any) -> str:
    """Return a deliverable MEDIA marker or an honest failure, never image base64."""
    from PIL import Image

    from agent import provider_media

    try:
        encoded = getattr(item, "result", None)
        if not isinstance(encoded, str) or not encoded:
            raise ValueError("missing image result")
        raw = base64.b64decode(encoded, validate=True)
        # Derive extensions from the bytes, not provider-controlled path fragments.
        with Image.open(BytesIO(raw)) as image:
            extension = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp", "GIF": "gif"}.get(image.format or "")
            if extension is None:
                raise ValueError("unsupported image format")
            image.verify()
        path = provider_media.save_bytes(
            "generated/images", raw, prefix="responses", extension=extension,
        )
    except (binascii.Error, ValueError, OSError, SyntaxError, Image.DecompressionBombError) as exc:
        # Provider payloads and filesystem exception text can contain private data.
        logger.warning("Could not attach Responses image (%s).", type(exc).__name__)
        return "The provider returned an image-generation result, but Hermes could not attach the image."
    return f"MEDIA:{path}"
