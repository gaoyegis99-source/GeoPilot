"""Generate T04 probe coordinates and certified distance brackets using GeographicLib."""

import heapq
import json
import math

import geographiclib
from geographiclib.geodesic import Geodesic


GEODESIC = Geodesic.WGS84
DISTANCE_BRACKET_M = 0.02
PROBE_OFFSET_M = 0.25
ANALYTIC_BOUNDARY_TOLERANCE_M = 0.05


def geodesic_lines(coordinates):
    return [
        GEODESIC.InverseLine(lat1, lon1, lat2, lon2)
        for (lon1, lat1), (lon2, lat2) in zip(coordinates[:-1], coordinates[1:], strict=True)
    ]


def distance_bracket(lines, point):
    """Bound the minimum along the continuous segments, without constructing a buffer."""
    intervals = []
    upper = math.inf

    def distance(line, position):
        sample = line.Position(position)
        return GEODESIC.Inverse(point[1], point[0], sample["lat2"], sample["lon2"])["s12"]

    def add_interval(index, left, right, at_left, at_right):
        # Distance from a fixed point is 1-Lipschitz along the source geodesic.
        lower = max(0.0, (at_left + at_right - (right - left)) / 2 - 1e-7)
        heapq.heappush(intervals, (lower, index, left, right, at_left, at_right))

    for index, line in enumerate(lines):
        at_start, at_end = distance(line, 0.0), distance(line, line.s13)
        upper = min(upper, at_start, at_end)
        add_interval(index, 0.0, line.s13, at_start, at_end)
    while upper - intervals[0][0] > DISTANCE_BRACKET_M - 2e-7:
        _, index, left, right, at_left, at_right = heapq.heappop(intervals)
        middle = (left + right) / 2
        at_middle = distance(lines[index], middle)
        upper = min(upper, at_middle)
        add_interval(index, left, middle, at_left, at_middle)
        add_interval(index, middle, right, at_middle, at_right)
    return [intervals[0][0], upper + 1e-7]


def normal_ray(lines, segment, fraction, angle):
    line = lines[segment]
    start = line.Position(fraction * line.s13)
    return (start["lon2"], start["lat2"]), start["azi2"] + angle


def probe_pair(lines, distance, label, origin, azimuth, boundary_radius=None):
    radius = distance if boundary_radius is None else boundary_radius
    probes = []
    for covered, delta in ((True, -PROBE_OFFSET_M), (False, PROBE_OFFSET_M)):
        found = GEODESIC.Direct(origin[1], origin[0], azimuth, radius + delta)
        point = [found["lon2"], found["lat2"]]
        lower, upper = distance_bracket(lines, point)
        if (covered and upper >= distance) or (not covered and lower <= distance):
            raise ValueError(f"Reference cannot classify {label!r} at radius {radius + delta}")
        probes.append({"label": label, "point": point, "covered": covered, "distance_bracket_m": [lower, upper]})
    return probes


def reference():
    straight = [[117.0, 0.0], [117.02, 0.0]]
    straight_lines = geodesic_lines(straight)
    straight_probes = [
        *probe_pair(straight_lines, 500, "north side", *normal_ray(straight_lines, 0, 0.437, -90)),
        *probe_pair(straight_lines, 500, "south side", *normal_ray(straight_lines, 0, 0.663, 90)),
        *probe_pair(straight_lines, 500, "start round cap", straight[0], 229.5),
        *probe_pair(straight_lines, 500, "end round cap", straight[-1], 63.5),
    ]

    bend = [[117.0, 0.0], [117.02, 0.0], [117.02, 0.02]]
    bend_lines = geodesic_lines(bend)
    bend_probes = [
        *probe_pair(bend_lines, 500, "outer round corner", bend[1], 135.5),
        *probe_pair(bend_lines, 500, "inner corner", bend[1], 315.0, 500 * math.sqrt(2)),
        *probe_pair(bend_lines, 500, "second segment side", *normal_ray(bend_lines, 1, 0.61, 90)),
    ]

    high_latitude = [[12.0, 65.0], [14.0, 65.0]]
    high_lines = geodesic_lines(high_latitude)
    midpoint, north = normal_ray(high_lines, 0, 0.537, -90)
    high_probes = probe_pair(high_lines, 1000, "north of curved geodesic", midpoint, north)
    lon, lat = high_probes[0]["point"]
    raw_chord_distance = GEODESIC.Inverse(lat, lon, 65.0, lon)["s12"]
    return {
        "reference": f"GeographicLib {geographiclib.__version__} WGS84",
        "distance_bracket_tolerance_m": DISTANCE_BRACKET_M,
        "probe_radial_offset_m": PROBE_OFFSET_M,
        "analytic_boundary_tolerance_m": ANALYTIC_BOUNDARY_TOLERANCE_M,
        "production_sampling": {"max_segment_step_m": 25, "max_cap_angle_deg": 1},
        "cases": [
            {"id": "equatorial-straight", "distance_m": 500, "line": straight, "probes": straight_probes},
            {"id": "equatorial-bend", "distance_m": 500, "line": bend, "probes": bend_probes},
            {
                "id": "high-latitude-arc", "distance_m": 1000, "line": high_latitude, "probes": high_probes,
                "centerline_point": list(midpoint), "inside_probe_raw_chord_distance_m": raw_chord_distance,
            },
        ],
    }


if __name__ == "__main__":
    print(json.dumps(reference(), indent=2))
