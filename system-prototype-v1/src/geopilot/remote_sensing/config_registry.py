"""Resolve one exact versioned sensor configuration for the observed product."""

from collections.abc import Iterable
from pathlib import Path

import yaml

from geopilot.methods.loader import _UniqueKeySafeLoader
from geopilot.remote_sensing.config_models import SensorConfigRef, SensorProductConfig, SensorProductKey


class SensorConfigLookupError(LookupError):
    def __init__(self, key: SensorProductKey, matches: tuple[SensorConfigRef, ...] = ()) -> None:
        self.key = key
        self.matches = matches
        self.code = "SENSOR_CONFIG_AMBIGUOUS" if matches else "SENSOR_CONFIG_NOT_FOUND"
        selector = f"{key.sensor}/{key.product_level}/{key.processing_version}"
        if matches:
            references = ", ".join(f"{ref.id}@{ref.version}" for ref in matches)
            super().__init__(f"Multiple sensor configurations match {selector}: {references}")
        else:
            super().__init__(f"No sensor configuration matches {selector}")


class SensorConfigLoadError(ValueError):
    def __init__(self, path: Path, message: str) -> None:
        super().__init__(f"{path}: {message}")
        self.path = path


class SensorConfigRegistry:
    """An isolated snapshot indexed by product key and configuration version."""

    def __init__(self, configurations: Iterable[SensorProductConfig]) -> None:
        self._configurations: dict[SensorConfigRef, SensorProductConfig] = {}
        self._products: dict[SensorProductKey, list[SensorConfigRef]] = {}
        for candidate in configurations:
            configuration = SensorProductConfig.model_validate(candidate)
            reference = configuration.ref
            if reference in self._configurations:
                raise ValueError(f"Sensor configuration {reference.id}@{reference.version} is defined more than once")
            self._configurations[reference] = configuration
            self._products.setdefault(configuration.key, []).append(reference)

    def get(self, reference: SensorConfigRef) -> SensorProductConfig:
        """Read the exact recorded configuration version."""
        return self._configurations[SensorConfigRef.model_validate(reference)].model_copy(deep=True)

    def lookup(self, key: SensorProductKey) -> SensorProductConfig:
        """Return the unique configuration for the full metadata key."""
        key = SensorProductKey.model_validate(key)
        matches = self._products.get(key, [])
        if len(matches) != 1:
            ordered = tuple(sorted(matches, key=lambda reference: (reference.id, reference.version)))
            raise SensorConfigLookupError(key, ordered)
        return self.get(matches[0])


def load_sensor_registry(directory: str | Path) -> SensorConfigRegistry:
    """Read the immediate YAML files with strict keys and version references."""
    configurations: dict[SensorConfigRef, SensorProductConfig] = {}
    for path in sorted(Path(directory).iterdir()):
        if path.suffix not in {".yaml", ".yml"} or path.is_dir():
            continue
        try:
            if not path.is_file():
                raise OSError("Expected a regular sensor configuration file")
            document = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeySafeLoader)
            configuration = SensorProductConfig.model_validate(document)
        except (OSError, ValueError, RecursionError, yaml.YAMLError) as error:
            raise SensorConfigLoadError(path, str(error)) from error
        reference = configuration.ref
        if reference in configurations:
            raise SensorConfigLoadError(
                path, f"Sensor configuration {reference.id}@{reference.version} is defined more than once",
            )
        configurations[reference] = configuration
    return SensorConfigRegistry(configurations.values())
