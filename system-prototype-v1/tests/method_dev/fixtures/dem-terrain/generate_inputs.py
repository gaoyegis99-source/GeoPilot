"""Transfer V1's authored DEM plane to the EPSG:6933 reference grid."""

import argparse
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import rasterio


SOURCE = Path(__file__).resolve().parents[3] / "fixtures/raster/dem-plane.tif"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="existing directory with unused DEM filenames")
    args = parser.parse_args()
    targets = {args.output / "dem.tif": "m", args.output / "unlabelled-dem.tif": None}
    if any(path.exists() or path.is_symlink() for path in targets):
        parser.error("DEM fixture destinations must not already exist")
    with rasterio.open(SOURCE) as source, TemporaryDirectory(dir=args.output) as temporary:
        # Read the already-authored input plane. Neither output reference is calculated here.
        pixels = source.read()
        profile = {**source.profile, "crs": "EPSG:6933"}
        for path, unit in targets.items():
            stage = Path(temporary) / path.name
            with rasterio.open(stage, "w", **profile) as target:
                target.write(pixels)
                target.set_band_description(1, "elevation")
                if unit is not None:
                    target.set_band_unit(1, unit)
            os.link(stage, path)


if __name__ == "__main__":
    main()
