import io
from pathlib import Path

import pytest
from PIL import Image

from workflow.images import prepare_image, render_svg

SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 80 120"><rect width="80" height="120" fill="red"/></svg>'


def test_svg_is_rendered_as_png_with_correct_pixels():
    data = prepare_image(SVG)
    with Image.open(io.BytesIO(data)) as image:
        assert image.format == "PNG" and image.size == (80, 120)
        assert image.convert("RGB").getpixel((40, 60)) == (255, 0, 0)


def test_svg_dimensions_are_bounded():
    data = render_svg(SVG.replace(b"80 120", b"80000 120000"))
    with Image.open(io.BytesIO(data)) as image:
        assert image.size == (1365, 2048)


@pytest.mark.parametrize(
    "data",
    [
        b'<svg xmlns="http://www.w3.org/2000/svg" width="NaN" height="12"/>',
        b'<!DOCTYPE svg [<!ENTITY x "test">]><svg/>',
        b'<svg xmlns="http://www.w3.org/2000/svg"><image href="file:///etc/passwd"/></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg"><image href="https://example.com/image.png"/></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg"><style>@import "https://example.com";</style></svg>',
    ],
)
def test_unsafe_or_invalid_svg_is_rejected(data):
    with pytest.raises(ValueError):
        render_svg(data)


@pytest.mark.parametrize("format", ["PNG", "JPEG", "BMP", "WEBP", "GIF"])
def test_raster_formats_are_automatically_prepared_as_png(format):
    original = io.BytesIO()
    Image.new("RGB", (9, 13), "red").save(original, format=format)
    data = prepare_image(original.getvalue())
    with Image.open(io.BytesIO(data)) as image:
        assert image.format == "PNG" and image.size == (9, 13)


def test_non_image_is_not_uploaded():
    with pytest.raises((OSError, ValueError)):
        prepare_image(b"<html>not an image</html>")


def test_image_settings_are_no_longer_exposed():
    import yaml

    from workflow.config import Config

    settings = yaml.safe_load(Path("StashSystemSync.yml").read_text())["settings"]
    for key in ["reencode_images", "image_upload_format", "ffmpeg_path"]:
        assert "performer_" + key not in settings
        assert key not in Config().sections["performer"]


def test_exif_orientation_is_applied_automatically():
    original = io.BytesIO()
    image = Image.new("RGB", (9, 13), "red")
    exif = image.getexif()
    exif[274] = 6
    image.save(original, format="JPEG", exif=exif)
    with Image.open(io.BytesIO(prepare_image(original.getvalue()))) as result:
        assert result.size == (13, 9)
        assert result.getexif().get(274, 1) == 1


def test_server_image_preview_does_not_modify_or_enqueue(setup, monkeypatch):
    from plugin import run
    from workflow.services import Performer

    stash, config, state, root = setup
    monkeypatch.setattr(
        Performer, "read", lambda self, pid: {"id": pid, "image_path": "/image.svg"}
    )
    stash.bytes = lambda _: SVG
    before = run({"args": {"mode": "status"}}, state.directory)
    result = run({"args": {"mode": "image_preview", "performer_id": "7"}}, state.directory, stash)
    assert result["source_svg"] and result["content_type"] == "image/png"
    assert result["dimensions"] == [80, 120]
    assert run({"args": {"mode": "status"}}, state.directory) == before


def test_svg_internal_gradient_reference_is_rendered():
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="30"><defs><linearGradient id="g"><stop stop-color="red"/></linearGradient></defs><rect width="20" height="30" style="fill:url(&quot;#g&quot;)"/></svg>'
    assert render_svg(svg).startswith(b"\x89PNG")


def test_cmyk_jpeg_is_automatically_normalized():
    source = io.BytesIO()
    Image.new("CMYK", (9, 13), (0, 255, 255, 0)).save(source, format="JPEG")
    with Image.open(io.BytesIO(prepare_image(source.getvalue()))) as result:
        assert result.format == "PNG" and result.mode == "RGB"
