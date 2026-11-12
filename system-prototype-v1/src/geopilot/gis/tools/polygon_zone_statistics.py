"""Write polygon allocation details and a deduplicated summary."""

import csv
import json
import os
from contextlib import ExitStack
from dataclasses import asdict, fields
from pathlib import Path

from geopilot.execution.output_staging import temporary_output_directory
from geopilot.gis.tools.tool_command import run_tool_command
from geopilot.gis.tools.vector_step_files import read_vector
from geopilot.gis.vector.polygon_statistics import PolygonAllocation, PolygonStatistics, aggregate_polygon_statistics


def _write_statistics(outputs: dict, statistics: PolygonStatistics) -> None:
    with ExitStack() as staging:
        files = {}
        for role, suffix in (("allocation_details", ".csv"), ("zone_statistics", ".json")):
            reference = outputs[role]
            target = Path(reference["path"])
            if target.suffix.lower() != suffix or reference.get("layer") is not None:
                raise ValueError(f"{role} requires a {suffix} path without a layer")
            directory = staging.enter_context(temporary_output_directory(target, prefix=".polygon-statistics-"))
            files[role] = (Path(directory) / target.name, target)

        with files["allocation_details"][0].open("x", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=[field.name for field in fields(PolygonAllocation)])
            writer.writeheader()
            writer.writerows(asdict(row) for row in statistics.details)
        with files["zone_statistics"][0].open("x", encoding="utf-8") as stream:
            json.dump(statistics.summary, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")

        with ExitStack() as rollback:
            for temporary, target in files.values():
                os.link(temporary, target)
                rollback.callback(target.unlink, missing_ok=True)
            rollback.pop_all()


def polygon_zone_statistics(request: dict) -> dict:
    parameters = request["parameters"]
    if parameters["area_unit"] != "square_meter":
        raise ValueError("area_unit must be square_meter")
    inputs = request["inputs"]
    targets, _ = read_vector(inputs["targets"], role="targets", require_source_id=True)
    originals, _ = read_vector(inputs["original_targets"], role="original_targets", require_source_id=True)
    zones, _ = read_vector(inputs["zones"], role="zones", require_source_id=True)
    result = aggregate_polygon_statistics(
        targets, originals, zones, boundary_assignment=parameters["boundary_assignment"],
        boundary_tie=parameters["boundary_tie"], area_crs=parameters["area_crs"],
    )
    outputs = request["output_targets"]
    _write_statistics(outputs, result)
    counts = result.summary["counts"]
    print(f"Summarized {counts['total']} unique target(s), {counts['unassigned']} unassigned")
    return outputs


if __name__ == "__main__":
    raise SystemExit(run_tool_command(polygon_zone_statistics, description=__doc__))
