"""Validate method step order before compiling execution requests."""

from collections.abc import Iterable
from itertools import zip_longest

from geopilot.methods.models import MethodDefinition, MethodStep
from geopilot.plans.dependency_validator import DependencyIssue, validate_dependencies
from geopilot.plans.models import PlanStep


def validate_step_order(
    method: MethodDefinition, steps: Iterable[MethodStep | PlanStep]
) -> tuple[DependencyIssue, ...]:
    """Check the fixed sequence and its declared dependencies without reordering it."""
    ordered = tuple(steps)
    order_issues: tuple[DependencyIssue, ...] = ()
    if method.locked_order:
        expected_keys = (step.key for step in method.steps)
        actual_keys = (step.key for step in ordered)
        for index, (expected, actual) in enumerate(zip_longest(expected_keys, actual_keys)):
            if expected != actual:
                expected_step = repr(expected) if expected is not None else "the end of the step list"
                actual_step = repr(actual) if actual is not None else "the end of the step list"
                order_issues = (DependencyIssue(
                    "LOCKED_STEP_ORDER", f"steps.{index}.key",
                    f"Locked method {method.id!r} expects {expected_step}, received {actual_step}",
                ),)
                break

    return order_issues + validate_dependencies(method.inputs, ordered)
