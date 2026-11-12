"""Reject configuration updates that break existing exact GeoPilot versions."""

import argparse
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from scripts.quality.validate_config_schemas import check_methods, check_sensors, check_tools, read_yaml

from geopilot.api.server import _qgis_bindings, _yaml_files
from geopilot.execution.qgis_runner import _validate_binding
from geopilot.methods.loader import load_method_catalog
from geopilot.remote_sensing.config_models import SensorProductConfig


@dataclass(frozen=True)
class VersionedConfig:
    path: str
    fingerprint: str
    binding: str | None = None


def fingerprint(value) -> str:
    canonical = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return sha256(canonical.encode("ascii")).hexdigest()


def snapshot(root: Path, tree: str) -> tuple[dict, list[dict]]:
    findings = []
    registry, _ = check_tools(root, findings)
    check_methods(root, registry, findings, [])
    check_sensors(root, findings)
    if findings:
        return {}, [{"tree": tree, **asdict(finding)} for finding in findings]

    config = root / "config"
    bindings = _qgis_bindings(config / "runner_bindings")
    versions = {}
    for method in load_method_catalog(config / "methods").list():
        versions["method", method.id, method.version] = VersionedConfig(
            "config/methods", fingerprint(method.model_dump(mode="json")),
        )
    for tool in registry.list():
        contract = tool.contract
        binding_fingerprint = None
        if contract.runner == "qgis":
            binding, _ = _validate_binding(contract, bindings)
            binding_fingerprint = fingerprint({
                "inputs": binding["inputs"], "parameters": binding["parameters"],
                "outputs": binding["outputs"], "enum_values": binding.get("enum_values", {}),
            })
        versions["tool", contract.id, contract.version] = VersionedConfig(
            "config/tools", fingerprint(contract.model_dump(mode="json")), binding_fingerprint,
        )
    for file in _yaml_files(config / "sensors"):
        sensor = SensorProductConfig.model_validate(read_yaml(file))
        versions["sensor", sensor.id, sensor.version] = VersionedConfig(
            file.relative_to(root).as_posix(), fingerprint(sensor.model_dump(mode="json")),
        )
    return versions, []


def reference(key: tuple[str, str, int]) -> dict:
    family, identifier, version = key
    return {"family": family, "id": identifier, "version": version}


def check_versions(baseline: Path, candidate: Path) -> dict:
    """Compare validated registries without writing config or invoking GIS."""
    previous, old_issues = snapshot(baseline, "baseline")
    current, new_issues = snapshot(candidate, "candidate")
    issues = old_issues + new_issues
    report = {"schema_version": 1, "status": "FAIL", "preserved_versions": 0, "added": [], "issues": issues}
    if issues:
        return report

    for key, entry in sorted(previous.items()):
        proposed = current.get(key)
        if proposed is None:
            code, path = "VERSION_REMOVED", entry.path
        elif proposed.fingerprint != entry.fingerprint:
            code, path = "VERSION_REDEFINED", proposed.path
        elif proposed.binding != entry.binding:
            code, path = "VERSION_BINDING_CHANGED", "config/runner_bindings"
        else:
            report["preserved_versions"] += 1
            continue
        issues.append({
            "tree": "candidate", "path": path, "code": code, "field": "version", "subject": reference(key),
        })
    report["added"] = [reference(key) for key in sorted(current.keys() - previous.keys())]
    report["status"] = "FAIL" if issues else "PASS"
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--baseline", required=True, type=Path, help="released prototype root containing config/")
    parser.add_argument("--candidate", required=True, type=Path, help="candidate prototype root containing config/")
    args = parser.parse_args()
    report = check_versions(args.baseline, args.candidate)
    print(json.dumps(report, indent=2, allow_nan=False))
    return int(report["status"] != "PASS")


if __name__ == "__main__":
    raise SystemExit(main())
