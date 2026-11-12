"""Crop the staged DEM to its verified neighborhood window."""

import json
from pathlib import Path

import rasterio

from geopilot.gis.raster.dem_prepare import prepare_dem
from geopilot.gis.tools.tool_command import run_tool_command


def prepare_dem_window(request: dict) -> dict:
    source = request["inputs"]["dem"]["path"]
    windows = request["inputs"]["windows"]["path"]
    target = request["output_targets"]["prepared_dem"]
    record = json.loads(Path(windows).read_text(encoding="utf-8"))
    metadata = prepare_dem(source, target["path"], record)
    evidence = {
        "path": target["path"], "windows": windows, "metadata": metadata.model_dump(mode="json"),
        "rasterio": rasterio.__version__, "gdal": rasterio.__gdal_version__,
    }
    print("prepared_dem: " + json.dumps(evidence, ensure_ascii=False, allow_nan=False))
    return {"prepared_dem": target}


if __name__ == "__main__":
    raise SystemExit(run_tool_command(prepare_dem_window, description=__doc__))
