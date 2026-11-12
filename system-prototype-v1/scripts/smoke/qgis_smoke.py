"""Exercise QGIS Processing on a fixed, manually defined projected road."""

import argparse
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import geopandas as gpd
import pyogrio
from shapely.geometry import LineString


DEFAULT_OUTPUT_ROOT = Path(__file__).resolve().parents[2] / "artifacts" / "smoke" / "qgis"
ALGORITHM = "native:buffer"
CRS_EPSG = 32650
ROAD_ID = "ROAD-1"
START = (500_000, 4_500_000)
END = (501_000, 4_500_000)
DISTANCE_M = 25
SEGMENTS = 8
TIMEOUT_S = 40


def run_qgis(run_dir, commands, name, executable, *parameters):
    args = [executable, *parameters]
    log = run_dir / f"{name}.log"
    command = {"args": args, "exit_code": None, "log": log.name}
    commands[name] = command
    env = os.environ.copy()
    env.setdefault("QT_QPA_PLATFORM", "offscreen")

    with log.open("w", encoding="utf-8") as stream:
        stream.write(f"$ {shlex.join(args)}\n")
        stream.flush()
        try:
            result = subprocess.run(
                args, cwd=run_dir, env=env, stdout=stream, stderr=subprocess.STDOUT,
                timeout=TIMEOUT_S, check=False,
            )
        except OSError as error:
            stream.write(f"\n启动错误：{error}\n")
            raise RuntimeError(f"无法启动 QGIS ({name})，请检查可执行入口和依赖：{log}") from error
        except subprocess.TimeoutExpired as error:
            stream.write(f"\n超过 {TIMEOUT_S} 秒\n")
            raise RuntimeError(f"QGIS {name} 超时：{log}") from error
        command["exit_code"] = result.returncode
        stream.write(f"\nexit_code: {result.returncode}\n")
    if result.returncode != 0:
        raise RuntimeError(f"QGIS {name} 退出 {result.returncode}，请检查日志中的 provider 或环境错误：{log}")
    return log.read_text(encoding="utf-8", errors="replace")


def verify_buffer(path):
    if not path.is_file():
        raise ValueError(f"QGIS 退出成功但未写出成果：{path}")
    layers = pyogrio.list_layers(path)
    if len(layers) != 1:
        raise ValueError(f"预期一个缓冲图层，实际 {len(layers)} 个")
    layer = str(layers[0, 0])
    buffered = pyogrio.read_dataframe(path, layer=layer)
    if buffered.crs is None or buffered.crs.to_epsg() != CRS_EPSG:
        raise ValueError(f"缓冲图层 CRS 应为 EPSG:{CRS_EPSG}，实际 {buffered.crs}")
    if "road_id" not in buffered or buffered["road_id"].tolist() != [ROAD_ID]:
        raise ValueError("缓冲应保留唯一道路 ROAD-1")
    geometry = buffered.geometry.iloc[0]
    if (geometry is None or geometry.geom_type not in {"Polygon", "MultiPolygon"}
            or geometry.is_empty or not geometry.is_valid):
        raise ValueError("缓冲几何必须为有效非空面")

    length_m = math.dist(START, END)
    sides = 4 * SEGMENTS
    # The two semicircle caps make a regular polygon of `sides` sides.
    cap_area = sides / 2 * DISTANCE_M**2 * math.sin(math.tau / sides)
    expected_area = 2 * DISTANCE_M * length_m + cap_area
    expected_bounds = (START[0] - DISTANCE_M, START[1] - DISTANCE_M,
                       END[0] + DISTANCE_M, END[1] + DISTANCE_M)
    area_tolerance = 1e-5
    bounds_tolerance = 1e-6
    if not math.isclose(geometry.area, expected_area, abs_tol=area_tolerance, rel_tol=0):
        raise ValueError(f"缓冲面积 {geometry.area} m²，应为 {expected_area} ± {area_tolerance} m²")
    if any(not math.isclose(actual, expected, abs_tol=bounds_tolerance, rel_tol=0)
           for actual, expected in zip(geometry.bounds, expected_bounds)):
        raise ValueError(f"缓冲范围 {geometry.bounds}，应为 {expected_bounds}（米）")

    return {
        "file": path.name, "layer": layer, "crs": f"EPSG:{CRS_EPSG}",
        "road_id": ROAD_ID, "geometry_type": geometry.geom_type,
        "area_m2": geometry.area, "expected_area_m2": expected_area,
        "area_tolerance_m2": area_tolerance, "bounds_m": geometry.bounds,
        "expected_bounds_m": expected_bounds, "bounds_tolerance_m": bounds_tolerance,
    }


def smoke(run_dir, executable):
    commands = {}
    summary = {"status": "FAIL", "commands": commands}
    try:
        run_qgis(run_dir, commands, "version", executable, "--version")
        algorithms = run_qgis(run_dir, commands, "list", executable, "list")
        if not re.search(rf"^\s*{ALGORITHM}(?:\s|$)", algorithms, re.MULTILINE):
            raise RuntimeError(f"算法列表缺少 {ALGORITHM}，请检查 native provider：{run_dir / 'list.log'}")
        help_text = run_qgis(run_dir, commands, "help", executable, "help", ALGORITHM)
        if not all(parameter in help_text for parameter in ("INPUT", "DISTANCE", "SEGMENTS", "OUTPUT")):
            raise RuntimeError(f"{ALGORITHM} 参数帮助不完整：{run_dir / 'help.log'}")

        roads = gpd.GeoDataFrame({"road_id": [ROAD_ID]}, geometry=[LineString([START, END])],
                                 crs=f"EPSG:{CRS_EPSG}")
        pyogrio.write_dataframe(roads, run_dir / "roads.gpkg", layer="roads", driver="GPKG")
        summary["input"] = {"file": "roads.gpkg", "layer": "roads", "road_id": ROAD_ID,
                            "crs": f"EPSG:{CRS_EPSG}", "linear_unit": "metre",
                            "coordinates": [START, END], "source": "人工定义的水平线段"}
        run_qgis(
            run_dir, commands, "buffer", executable, "run", ALGORITHM, "--", "INPUT=roads.gpkg",
            f"DISTANCE={DISTANCE_M}", f"SEGMENTS={SEGMENTS}", "END_CAP_STYLE=0", "JOIN_STYLE=0",
            "MITER_LIMIT=2", "DISSOLVE=false", "OUTPUT=buffer.gpkg",
        )
        summary["output"] = verify_buffer(run_dir / "buffer.gpkg")
        summary["status"] = "PASS"
    except Exception as error:
        summary["error"] = f"{type(error).__name__}: {error}"

    summary_file = run_dir / "summary.json"
    summary_file.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if summary["status"] == "FAIL":
        print(f"{summary['error']}\nFAIL: {summary_file}", file=sys.stderr)
        return 1
    print(f"PASS: {summary_file}")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qgis-process", default=os.getenv("GEOPILOT_QGIS_PROCESS") or "qgis_process",
                        help="qgis_process 可执行入口，也可用 GEOPILOT_QGIS_PROCESS 设置")
    parser.add_argument("--output-dir", type=Path, help="本次输出的新目录，默认自动创建")
    options = parser.parse_args()

    executable = os.path.expanduser(options.qgis_process)
    located = shutil.which(executable)
    if located or os.path.dirname(executable):
        executable = os.path.abspath(located or executable)

    if options.output_dir is None:
        DEFAULT_OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
        run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=DEFAULT_OUTPUT_ROOT))
    else:
        run_dir = options.output_dir.resolve()
        try:
            run_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            parser.error(f"输出目录已存在：{run_dir}")
    return smoke(run_dir, executable)


if __name__ == "__main__":
    sys.exit(main())
