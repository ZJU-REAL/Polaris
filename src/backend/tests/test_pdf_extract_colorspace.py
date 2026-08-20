"""Regression tests for PDF figure colorspace normalization."""

import io

import pymupdf
import pytest
from PIL import Image

from app.services.literature import pdf_extract


def _pixmap(colorspace):
    pix = pymupdf.Pixmap(colorspace, pymupdf.IRect(0, 0, 16, 16), 0)
    pix.clear_with(0)
    return pix


def test_pix_png_bytes_normalizes_cmyk():
    pix = _pixmap(pymupdf.csCMYK)

    encoded = pdf_extract._pix_png_bytes(pix)

    assert encoded.startswith(b"\x89PNG")
    with Image.open(io.BytesIO(encoded)) as image:
        assert image.mode == "RGB"
        assert image.size == (16, 16)


def test_pix_png_bytes_keeps_supported_rgb():
    pix = _pixmap(pymupdf.csRGB)

    assert pdf_extract._pix_png_bytes(pix) == pix.tobytes("png")


def test_pix_png_bytes_does_not_hide_unrelated_value_error():
    class BrokenPixmap:
        colorspace = object()

        def tobytes(self, _format):
            raise ValueError("invalid pixmap data")

    with pytest.raises(ValueError, match="invalid pixmap data"):
        pdf_extract._pix_png_bytes(BrokenPixmap())


def test_candidate_skips_pixmap_that_still_cannot_be_encoded():
    class BrokenPixmap:
        colorspace = None
        width = 16
        height = 16

        def tobytes(self, _format):
            raise ValueError("unsupported colorspace for 'png'")

    assert pdf_extract._candidate_from_pix(3, 0, BrokenPixmap()) is None
