"""Check declared step dependencies without changing their execution order."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter
from typing import Protocol

from geopilot.methods.models import AssetRoleBinding, StepOutputBinding


@dataclass(frozen=True, slots=True)
class DependencyIssue:
    code: str
    field: str
    message: str


class _DependencyStep(Protocol):
    @property
    def key(self) -> str: ...

    @property
    def inputs(self) -> Mapping[str, AssetRoleBinding | StepOutputBinding]: ...

    @property
    def outputs(self) -> Mapping[str, object]: ...


def validate_dependencies(
    input_roles: Iterable[str], steps: Iterable[_DependencyStep]
) -> tuple[DependencyIssue, ...]:
    """Validate role and named-output references in a method or expanded plan."""
    roles = frozenset(input_roles)
    ordered_steps = tuple(steps)
    declarations: dict[str, tuple[int, _DependencyStep]] = {}
    duplicates = set()
    for index, step in enumerate(ordered_steps):
        if step.key in declarations:
            duplicates.add(step.key)
        else:
            declarations[step.key] = (index, step)

    issues = []
    predecessors: dict[str, list[str]] = {key: [] for key in declarations if key not in duplicates}
    dependency_fields: dict[tuple[str, str], str] = {}
    for index, step in enumerate(ordered_steps):
        if declarations[step.key][0] != index:
            issues.append(DependencyIssue(
                "DUPLICATE_STEP_KEY", f"steps.{index}.key", f"Step key {step.key!r} was already declared",
            ))

        for port, binding in step.inputs.items():
            field = f"steps.{index}.inputs.{port}"
            if isinstance(binding, AssetRoleBinding):
                if binding.asset not in roles:
                    issues.append(DependencyIssue(
                        "UNKNOWN_ASSET_ROLE", f"{field}.asset", f"Unknown input asset role {binding.asset!r}",
                    ))
                continue

            reference = binding.step
            step_field = f"{field}.step"
            if reference == step.key:
                issues.append(DependencyIssue(
                    "SELF_STEP_REFERENCE", step_field, f"Step {step.key!r} cannot depend on itself",
                ))
                continue
            if reference not in declarations:
                issues.append(DependencyIssue("UNKNOWN_STEP", step_field, f"Unknown step {reference!r}"))
                continue
            if reference in duplicates:
                # A duplicate key has no unique output definition and carries its own issue.
                continue

            target_index, target = declarations[reference]
            if binding.output not in target.outputs:
                issues.append(DependencyIssue(
                    "UNKNOWN_OUTPUT", f"{field}.output", f"Step {reference!r} does not declare output {binding.output!r}",
                ))
                continue
            if target_index > index:
                issues.append(DependencyIssue(
                    "FUTURE_STEP_REFERENCE", step_field, f"Step {reference!r} is declared after {step.key!r}",
                ))
            if step.key not in duplicates:
                predecessors[step.key].append(reference)
                dependency_fields.setdefault((step.key, reference), step_field)

    # Graphlib supplies a cycle witness without using a sorted execution order.
    try:
        TopologicalSorter(predecessors).prepare()
    except CycleError as error:
        cycle = error.args[1]
        field = dependency_fields[(cycle[-1], cycle[-2])]
        issues.append(DependencyIssue("DEPENDENCY_CYCLE", field, f"Step dependency cycle: {' -> '.join(cycle)}"))
    return tuple(issues)
