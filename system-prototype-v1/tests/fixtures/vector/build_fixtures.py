"""Write the manually defined vector fixture to a new GeoPackage."""

import argparse
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import geopandas
import pyogrio
from shapely import from_wkt


SOURCE = Path(__file__).with_name("source.json")


def build_geopackage(output: Path) -> None:
    if output.suffix.lower() != ".gpkg":
        raise ValueError("output must end with .gpkg")
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"{output} already exists")

    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    with TemporaryDirectory(prefix=".vector-fixture-", dir=output.parent) as temporary:
        package = Path(temporary) / output.name
        for layer, records in source["layers"].items():
            frame = geopandas.GeoDataFrame(
                {"source_feature_id": [record["source_feature_id"] for record in records]},
                geometry=from_wkt([record["wkt"] for record in records]),
                crs=source["crs"],
            )
            pyogrio.write_dataframe(frame, package, layer=layer, driver="GPKG")
        # Publish all layers together and fail if the destination appeared during generation.
        os.link(package, output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="new .gpkg path in an existing directory")
    args = parser.parse_args()
    try:
        build_geopackage(args.output)
    except (FileExistsError, FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
