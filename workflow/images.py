import html
import io
import math
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# Server-specific wheels can be deployed privately without changing the container.
_dependencies = Path(__file__).resolve().parent.parent / "data" / "python"
if _dependencies.is_dir():
    sys.path.append(str(_dependencies))


def is_svg(data):
    if len(data) > 2_000_000 or b"<svg" not in data[:4096].lower():
        return False
    try:
        return ET.fromstring(data).tag.rsplit("}", 1)[-1].lower() == "svg"
    except ET.ParseError:
        return False


def _pillow_raster(data, output, max_size=None):
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(data)) as source:
        image = ImageOps.exif_transpose(source)
        if max_size:
            image.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
        if output == "JPEG":
            if "A" in image.getbands():
                background = Image.new("RGB", image.size, "white")
                background.paste(image, mask=image.getchannel("A"))
                image = background
            else:
                image = image.convert("RGB")
        if output == "PNG" and image.mode not in {"RGB", "RGBA", "L", "LA", "P", "I;16", "I"}:
            image = image.convert("RGB")
        target = io.BytesIO()
        image.save(target, format=output, **({"quality": 92} if output == "JPEG" else {}))
        return target.getvalue()


def convert_raster(data, output="JPEG", max_size=None):
    try:
        return _pillow_raster(data, output, max_size)
    except ImportError:
        codec = "png" if output == "PNG" else "mjpeg"
        filters = []
        if max_size:
            filters = [
                "-vf",
                f"scale=min({max_size}\\,iw):min({max_size}\\,ih):force_original_aspect_ratio=decrease",
            ]
        try:
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    "pipe:0",
                    "-frames:v",
                    "1",
                    *filters,
                    "-f",
                    "image2pipe",
                    "-c:v",
                    codec,
                    "pipe:1",
                ],
                input=data,
                capture_output=True,
                timeout=60,
                check=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(
                "Raster conversion failed; Pillow or working ffmpeg is required"
            ) from exc
        signature = b"\x89PNG\r\n\x1a\n" if output == "PNG" else b"\xff\xd8"
        if not result.stdout.startswith(signature):
            raise RuntimeError("Raster conversion returned invalid image data") from None
        return result.stdout


def render_svg(data):
    """Rasterize a self-contained SVG with bounded output dimensions."""
    if len(data) > 2_000_000 or re.search(rb"<!\s*(?:DOCTYPE|ENTITY)", data, re.I):
        raise ValueError("SVG is oversized or contains unsupported declarations")
    root = ET.fromstring(data)
    if root.tag.rsplit("}", 1)[-1].lower() != "svg":
        raise ValueError("Not an SVG image")
    for element in root.iter():
        for key, value in element.attrib.items():
            if key.rsplit("}", 1)[-1].lower() == "href" and not value.startswith(
                ("#", "data:image/")
            ):
                raise ValueError("SVG external resources are unsupported")
    markup = data.decode("utf-8-sig")
    urls = re.findall(r"url\(\s*([^)]*)\)", html.unescape(markup), re.I)
    if re.search(r"@import", markup, re.I) or any(
        not value.strip().strip("\"'").startswith(("#", "data:image/")) for value in urls
    ):
        raise ValueError("SVG external styles or resources are unsupported")
    box = root.get("viewBox", "").replace(",", " ").split()

    def dimension(value, fallback):
        try:
            return float(value.removesuffix("px"))
        except (ValueError, AttributeError):
            return fallback

    width = dimension(root.get("width"), float(box[2]) if len(box) == 4 else 300)
    height = dimension(root.get("height"), float(box[3]) if len(box) == 4 else 150)
    if not all(math.isfinite(value) and value > 0 for value in (width, height)):
        raise ValueError("SVG dimensions must be finite and positive")
    scale = min(1, 2048 / max(width, height))
    from resvg_py import svg_to_bytes

    result = svg_to_bytes(
        svg_string=markup,
        width=max(1, round(width * scale)),
        height=max(1, round(height * scale)),
        log_information=False,
    )
    if not result.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError("SVG renderer returned invalid PNG data")
    return result


def prepare_image(data):
    """Always produce a decoded, oriented, bounded PNG for Jellyfin."""
    if is_svg(data):
        return render_svg(data)
    return convert_raster(data, "PNG", max_size=4096)
