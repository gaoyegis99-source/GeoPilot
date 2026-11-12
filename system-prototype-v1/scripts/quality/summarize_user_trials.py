"""Summarize one anonymous GeoPilot workbench observation log."""

import argparse
from collections import Counter
from datetime import date
import json
from pathlib import Path
import re
from statistics import median
import sys


TASKS = ("register_assets", "select_method", "confirm_plan", "run_plan", "inspect_checks", "download_result")
OUTCOMES = {"completed", "incomplete", "not_attempted"}
RECOVERY = ("self_recovered", "assisted_recovery", "unresolved")
RECOMMENDED_SESSIONS = 5


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError(f"Invalid JSON numeric constant: {value}")


def require_fields(value, fields, path):
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected an object")
    expected = set(fields.split())
    missing = sorted(expected - value.keys())
    extra = sorted(value.keys() - expected)
    if missing or extra:
        raise ValueError(f"{path}: missing fields {missing}, unexpected fields {extra}")


def require_choice(value, choices, path):
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"{path}: expected one of {', '.join(sorted(choices))}")


def validate_task(task, path):
    require_fields(task, "outcome elapsed_seconds help_requests assisted errors", path)
    require_choice(task["outcome"], OUTCOMES, f"{path}.outcome")
    elapsed = task["elapsed_seconds"]
    if type(task["help_requests"]) is not int or task["help_requests"] < 0:
        raise ValueError(f"{path}.help_requests: expected a nonnegative integer")
    if type(task["assisted"]) is not bool:
        raise ValueError(f"{path}.assisted: expected a boolean")
    if not isinstance(task["errors"], list):
        raise ValueError(f"{path}.errors: expected an array")
    if task["outcome"] == "not_attempted":
        if elapsed is not None or task["help_requests"] or task["assisted"] or task["errors"]:
            raise ValueError(f"{path}: a task not attempted cannot contain elapsed time or events")
    elif type(elapsed) not in (int, float) or not 0 <= elapsed <= 86_400:
        raise ValueError(f"{path}.elapsed_seconds: expected a number between 0 and 86400")
    for index, error in enumerate(task["errors"]):
        error_path = f"{path}.errors[{index}]"
        require_fields(error, "code recovery", error_path)
        code = error["code"]
        if not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code):
            raise ValueError(f"{error_path}.code: expected a short uppercase error code")
        require_choice(error["recovery"], RECOVERY, f"{error_path}.recovery")
        if error["recovery"] == "assisted_recovery" and not task["assisted"]:
            raise ValueError(f"{error_path}: assisted recovery requires assisted=true")


def validate_session(session, path):
    require_fields(session, "participant_id trial_date gis_use environment tasks", path)
    participant = session["participant_id"]
    if not isinstance(participant, str) or not re.fullmatch(r"U[0-9]{3}", participant) or participant == "U000":
        raise ValueError(f"{path}.participant_id: expected U001–U999")
    trial_date = session["trial_date"]
    if not isinstance(trial_date, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", trial_date):
        raise ValueError(f"{path}.trial_date: expected YYYY-MM-DD")
    try:
        date.fromisoformat(trial_date)
    except ValueError as error:
        raise ValueError(f"{path}.trial_date: invalid calendar date") from error
    require_choice(session["gis_use"], ("occasional", "regular"), f"{path}.gis_use")
    environment = session["environment"]
    require_fields(environment, "commit os browser qgis", f"{path}.environment")
    commit = environment["commit"]
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError(f"{path}.environment.commit: expected a full Git SHA-1")
    for key in ("os", "browser", "qgis"):
        value = environment[key]
        if key == "os" and value is None:
            continue
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 120 or not value.isprintable():
            raise ValueError(f"{path}.environment.{key}: expected a short version label")

    tasks = session["tasks"]
    require_fields(tasks, " ".join(TASKS), f"{path}.tasks")
    stopped = False
    for name in TASKS:
        task = tasks[name]
        task_path = f"{path}.tasks.{name}"
        validate_task(task, task_path)
        if name == TASKS[0] and task["outcome"] == "not_attempted":
            raise ValueError(f"{task_path}: record a session only after the registration task starts")
        if stopped and task["outcome"] != "not_attempted":
            raise ValueError(f"{task_path}: an attempted task cannot follow an unattempted task")
        stopped = task["outcome"] == "not_attempted"


def load_observations(path):
    with path.open(encoding="utf-8") as stream:
        document = json.load(stream, object_pairs_hook=strict_object, parse_constant=reject_constant)
    require_fields(document, "schema_version record_kind sessions", "$")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise ValueError("$.schema_version: expected integer 1")
    require_choice(document["record_kind"], ("observed", "synthetic"), "$.record_kind")
    if not isinstance(document["sessions"], list):
        raise ValueError("$.sessions: expected an array")
    seen = set()
    for index, session in enumerate(document["sessions"]):
        session_path = f"$.sessions[{index}]"
        validate_session(session, session_path)
        participant = session["participant_id"]
        if participant in seen:
            raise ValueError(f"{session_path}.participant_id: duplicate participant {participant}")
        seen.add(participant)
    return document


def task_summary(rows):
    total = len(rows)
    attempted = [row for row in rows if row["outcome"] != "not_attempted"]
    completed = [row for row in attempted if row["outcome"] == "completed"]
    errors = [event for row in attempted for event in row["errors"]]
    recovery = Counter(event["recovery"] for event in errors)
    return {
        "participants": total,
        "attempted": len(attempted),
        "completed": len(completed),
        "completed_without_assistance": sum(not row["assisted"] for row in completed),
        "completion_rate": len(completed) / total if total else None,
        "participants_requesting_help": sum(row["help_requests"] > 0 for row in attempted),
        "help_requests": sum(row["help_requests"] for row in attempted),
        "assisted_participants": sum(row["assisted"] for row in attempted),
        "participants_with_errors": sum(bool(row["errors"]) for row in attempted),
        "errors": len(errors),
        "errors_by_code": dict(sorted(Counter(event["code"] for event in errors).items())),
        "recovery": {status: recovery[status] for status in RECOVERY},
        "median_elapsed_seconds_attempted": median(row["elapsed_seconds"] for row in attempted) if attempted else None,
    }


def summarize(document):
    sessions = document["sessions"]
    total = len(sessions)
    task_rows = [session["tasks"] for session in sessions]
    completed = [tasks for tasks in task_rows if all(task["outcome"] == "completed" for task in tasks.values())]
    kind = document["record_kind"]
    limits = [
        "One guided road-buffer sample with a convenience sample; results do not estimate the broader GIS user population."
    ]
    if total < RECOMMENDED_SESSIONS:
        limits.append(f"Fewer than {RECOMMENDED_SESSIONS} sessions.")
    if kind == "synthetic":
        limits.append("Synthetic records exercise the summarizer and are excluded from user research evidence.")
    return {
        "schema_version": 1,
        "record_kind": kind,
        "status": "SYNTHETIC" if kind == "synthetic" else ("OBSERVED" if total else "NOT_RUN"),
        "sessions": total,
        "recommended_minimum": RECOMMENDED_SESSIONS,
        "workflow": {
            "completed": len(completed),
            "completed_without_assistance": sum(
                not any(task["assisted"] for task in tasks.values()) for tasks in completed
            ),
            "completion_rate": len(completed) / total if total else None,
            "median_elapsed_seconds_completed": median(
                sum(task["elapsed_seconds"] for task in tasks.values()) for tasks in completed
            ) if completed else None,
        },
        "tasks": {name: task_summary([tasks[name] for tasks in task_rows]) for name in TASKS},
        "limitations": limits,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("observations", type=Path, help="UTF-8 JSON observation file")
    args = parser.parse_args()
    try:
        report = summarize(load_observations(args.observations))
    except (OSError, ValueError, RecursionError) as error:
        print(f"Invalid observation file: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, allow_nan=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
