"""Measure feature areas in an explicitly requested, preflight-checked projected CRS."""

import argparse
import csv
import json
import math
import platform
import sys
from pathlib import Path

import geopandas
import pyogrio
import pyproj
import shapely


def _areas(request: dict) -> tuple[list[tuple[str, float, str, str]], dict[str, str]]:
    source = request["inputs"]["source"]
    parameters = request["parameters"]
    area_crs = parameters["area_crs"]
    area_unit = parameters["area_unit"]
    if area_unit != "square_meter":
        raise ValueError("area_unit must be square_meter")

    frame = pyogrio.read_dataframe(source["path"], layer=source.get("layer"))
    expected = pyproj.CRS.from_user_input(area_crs)
    observed = frame.crs
    if observed is None or not observed.equals(expected, ignore_axis_order=True):
        raise ValueError(f"area_crs {area_crs} does not match the source CRS {observed}")
    horizontal = observed.to_2d()
    if not horizontal.is_projected or len(horizontal.axis_info) != 2 or any(
        axis.unit_name.casefold() not in {"metre", "meter"} or axis.unit_conversion_factor != 1
        for axis in horizontal.axis_info
    ):
        raise ValueError(f"area_crs requires projected metre axes, observed {observed}")

    if frame.empty or "source_feature_id" not in frame:
        raise ValueError("Input must contain polygons with source_feature_id values")
    source_ids = frame["source_feature_id"]
    ids = source_ids.astype(str)
    if source_ids.isna().any() or not ids.is_unique or ids.str.strip().eq("").any():
        raise ValueError("source_feature_id values must be nonempty and unique")

    geometry = frame.geometry
    if (
        not geometry.geom_type.isin({"Polygon", "MultiPolygon"}).all()
        or geometry.is_empty.any()
        or not geometry.is_valid.all()
    ):
        raise ValueError("Input contains nonpolygon, empty, or invalid geometry")
    areas = geometry.area.to_list()
    if any(not math.isfinite(area) for area in areas):
        raise ValueError("Computed polygon area is not finite")

    rows = [(identifier, area, area_unit, area_crs) for identifier, area in zip(ids, areas, strict=True)]
    target = request["output_targets"]["statistics"]
    return rows, {"path": target["path"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    args = parser.parse_args()
    try:
        request = json.loads(args.request.read_text(encoding="utf-8"))
        rows, output = _areas(request)
        with Path(output["path"]).open("x", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(("source_feature_id", "area", "area_unit", "area_crs"))
            writer.writerows(rows)

        manifest = {
            "outputs": {"statistics": output},
            "environment": {
                "python": platform.python_version(),
                "geopandas": geopandas.__version__,
                "pyogrio": pyogrio.__version__,
                "pyproj": pyproj.__version__,
                "shapely": shapely.__version__,
            },
        }
        with args.result.open("x", encoding="utf-8") as stream:
            json.dump(manifest, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
    except (OSError, ValueError, KeyError) as error:
        print(f"Area tool failed: {error}", file=sys.stderr)
        return 1
    print(f"Calculated {len(rows)} polygon area(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
