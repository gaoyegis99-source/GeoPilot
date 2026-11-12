"""Stage verified asset versions as run-relative file references."""

import errno
from collections.abc import Mapping
from pathlib import Path
from shutil import rmtree

from geopilot.data.assets import InputChangedError
from geopilot.data.errors import SourceReadError
from geopilot.data.fingerprint import copy_file_group
from geopilot.data.resolver import resolve_asset
from geopilot.domain.data_asset import AssetAvailability, DataAssetRef
from geopilot.domain.step_io import StepFileRef


def stage_run_inputs(
    database_path: str | Path, inputs: Mapping[str, DataAssetRef], workdir: Path
) -> dict[str, StepFileRef]:
    """Reopen exact asset versions and copy each source group once per run."""
    directory = workdir / "inputs"
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("The run input directory must be an existing local directory")
    resolved = {role: resolve_asset(database_path, ref) for role, ref in inputs.items()}
    copies: dict[tuple[Path, str], Path] = {}
    references = {}
    created = []
    try:
        for role, asset in resolved.items():
            if asset.availability != AssetAvailability.AVAILABLE:
                raise SourceReadError(f"Selected asset for {role} is unavailable", field=f"inputs.{role}")
            source = asset.source.path
            key = (source, asset.fingerprint)
            if key not in copies:
                location = directory / f"{len(copies) + 1:04d}"
                location.mkdir()
                created.append(location)
                destination = location / f"source{source.suffix.lower()}"
                try:
                    copy_file_group(source, destination, expected_digest=asset.fingerprint)
                except OSError as error:
                    if error.errno == errno.EAGAIN:
                        raise InputChangedError(f"Source changed while staging {role}: {source}") from error
                    raise
                copies[key] = destination
            path = copies[key].relative_to(workdir).as_posix()
            references[role] = StepFileRef(path=path, layer=asset.source.layer)
    except BaseException as error:
        for location in reversed(created):
            try:
                rmtree(location)
            except OSError as cleanup:
                error.add_note(f"Could not remove incomplete input directory {location}: {cleanup}")
        raise
    return references
