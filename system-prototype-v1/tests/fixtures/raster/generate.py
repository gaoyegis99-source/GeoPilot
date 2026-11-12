"""Generate the two small GeoTIFFs used by the P0 raster baseline."""

import argparse
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin


def write_raster(path, pixels, nodata, names, units):
    bands, rows, columns = pixels.shape
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=columns,
        height=rows,
        count=bands,
        dtype=pixels.dtype,
        crs="EPSG:32650",
        transform=from_origin(500000, 4400070, 10, 10),
        nodata=nodata,
    ) as dataset:
        dataset.write(pixels)
        for band, (name, unit) in enumerate(zip(names, units, strict=True), start=1):
            dataset.set_band_description(band, name)
            dataset.set_band_unit(band, unit)


def generate(directory):
    directory.mkdir(parents=True, exist_ok=True)
    dem_path = directory / "dem-plane.tif"
    spectral_path = directory / "two-band.tif"
    for path in (dem_path, spectral_path):
        if path.exists() or path.is_symlink():
            raise FileExistsError(f"Fixture path already exists: {path}")

    elevations = np.tile(np.arange(100, 170, 10, dtype="float32"), (7, 1))
    elevations[0, 0] = elevations[-1, -1] = -9999
    write_raster(dem_path, elevations[np.newaxis], -9999, ("elevation",), ("m",))

    rows, columns = np.indices((3, 4), dtype="uint16")
    red = 101 + 10 * rows + columns
    nir = red + 100
    red[1, 2] = nir[2, 0] = 65535
    write_raster(spectral_path, np.stack((red, nir)), 65535, ("red", "nir"), ("DN", "DN"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_directory", type=Path)
    generate(parser.parse_args().output_directory)
