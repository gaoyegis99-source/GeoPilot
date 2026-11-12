"""Read a step request and publish a Python worker's named output manifest."""

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path

import geopandas
import pyogrio
import pyproj
import shapely


def run_tool_command(operation: Callable[[dict], dict], *, description: str | None) -> int:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    try:
        request = json.loads(args.request.read_text(encoding="utf-8"))
        outputs = operation(request)
        manifest = {
            "outputs": outputs,
            "environment": {
                "geopandas": geopandas.__version__,
                "pyogrio": pyogrio.__version__,
                "pyproj": pyproj.__version__,
                "shapely": shapely.__version__,
            },
        }
        with args.result.open("x", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    except (KeyError, OSError, RuntimeError, TypeError, ValueError) as error:
        print(f"Tool failed: {error}", file=sys.stderr)
        return 1
    return 0
