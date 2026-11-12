"""Persist display previews in the run directory independently of formal artifacts."""

import json
import re
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import JsonValue

from geopilot.execution.step_support import resolve_step_path


_DIGEST = re.compile(r"[0-9a-f]{64}")
_PNG_HEADER = b"\x89PNG\r\n\x1a\n"


def _variant_path(
    artifact_id: str, source_fingerprint: str, palette: str, max_size: int, band: int, renderer_version: int,
) -> str:
    if "/" in artifact_id:
        raise ValueError("A preview requires a single registered artifact identifier")
    variant = json.dumps([source_fingerprint, palette, max_size, band, renderer_version], separators=(",", ":"))
    return f"previews/{artifact_id}/{sha256(variant.encode('utf-8')).hexdigest()}"


def _matches(
    descriptor: dict, artifact_id: str, source_fingerprint: str, palette: str, band: int,
) -> bool:
    return (
        descriptor.get("artifact_id") == artifact_id and descriptor.get("source_fingerprint") == source_fingerprint
        and descriptor.get("palette") == palette and descriptor.get("band") == band
    )


def load_preview(
    workdir: Path, artifact_id: str, *, source_fingerprint: str, palette: str, max_size: int, band: int,
    renderer_version: int,
) -> tuple[dict[str, JsonValue], bytes] | None:
    """Read a completed variant and verify the image referenced by its record."""
    variant = _variant_path(artifact_id, source_fingerprint, palette, max_size, band, renderer_version)
    record_path = resolve_step_path(workdir, f"{variant}/preview.json", field="preview.record")
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("renderer_version") != renderer_version or record.get("max_size") != max_size:
        return None
    descriptor, digest = record.get("descriptor"), record.get("image_sha256")
    if (
        not isinstance(descriptor, dict) or not _matches(descriptor, artifact_id, source_fingerprint, palette, band)
        or not isinstance(digest, str) or not _DIGEST.fullmatch(digest)
    ):
        return None
    image_path = resolve_step_path(workdir, f"{variant}/{digest}.png", field="preview.image")
    try:
        png = image_path.read_bytes()
    except FileNotFoundError:
        return None
    if not png.startswith(_PNG_HEADER) or sha256(png).hexdigest() != digest:
        return None
    return descriptor, png


def publish_preview(
    workdir: Path, artifact_id: str, *, source_fingerprint: str, palette: str, max_size: int, band: int,
    renderer_version: int, descriptor: dict[str, JsonValue], png: bytes,
) -> None:
    """Write the PNG before publishing the record that makes the pair readable."""
    if not _matches(descriptor, artifact_id, source_fingerprint, palette, band) or not png.startswith(_PNG_HEADER):
        raise ValueError("Preview data do not match the registered artifact and requested display settings")
    variant = _variant_path(artifact_id, source_fingerprint, palette, max_size, band, renderer_version)
    digest = sha256(png).hexdigest()
    record_path = resolve_step_path(workdir, f"{variant}/preview.json", field="preview.record")
    image_path = resolve_step_path(workdir, f"{variant}/{digest}.png", field="preview.image")
    record = json.dumps({
        "renderer_version": renderer_version, "max_size": max_size, "image_sha256": digest, "descriptor": descriptor,
    }, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    record_path.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".render-", dir=record_path.parent) as directory:
        temporary_image = Path(directory) / image_path.name
        temporary_record = Path(directory) / record_path.name
        temporary_image.write_bytes(png)
        temporary_record.write_text(record + "\n", encoding="utf-8")
        temporary_image.replace(image_path)
        temporary_record.replace(record_path)
