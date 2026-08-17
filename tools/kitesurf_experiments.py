#!/usr/bin/env python3
"""List, render, validate, or run the versioned Kitesurf experiment recipes."""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import shlex
import subprocess
import sys
from typing import Any


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "config/kitesurf_experiments.json"
PLACEHOLDER = re.compile(r"\{\{([a-z][a-z0-9_]*)\}\}")
ALLOWED_LAUNCHERS = {"python3", "node", "bash"}


class RecipeError(ValueError):
    pass


def load_manifest() -> dict[str, Any]:
    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if payload.get("schema") != "experimental.kitesurf_recipes.v1":
        raise RecipeError(f"unsupported manifest schema in {MANIFEST_PATH}")
    recipes = payload.get("recipes")
    if not isinstance(recipes, list) or not recipes:
        raise RecipeError("manifest recipes must be a non-empty list")
    return payload


def recipe_by_id(manifest: dict[str, Any], recipe_id: str) -> dict[str, Any]:
    matches = [item for item in manifest["recipes"] if item.get("id") == recipe_id]
    if len(matches) != 1:
        raise RecipeError(f"unknown or duplicate recipe id: {recipe_id}")
    return matches[0]


def parse_assignments(values: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        name, separator, assigned = value.partition("=")
        if not separator or not name or not assigned:
            raise RecipeError("--set must use NAME=VALUE with a non-empty value")
        parsed[name] = assigned
    return parsed


def selected_task_args(recipe: dict[str, Any], values: dict[str, str]) -> list[str]:
    selection = recipe.get("task_selection")
    if not selection:
        return []
    source_name = str(selection["source_variable"])
    source_value = values.get(source_name)
    if not source_value:
        raise RecipeError(f"recipe requires --set {source_name}=PATH")
    source = pathlib.Path(source_value).expanduser()
    if not source.is_absolute():
        source = REPO_ROOT / source
    if not source.is_file():
        raise RecipeError(f"task-selection results file is not readable: {source}")
    statuses = set(map(str, selection["statuses"]))
    rows: list[dict[str, Any]] = []
    task_ids: list[str] = []
    for line_number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RecipeError(f"invalid JSONL at {source}:{line_number}: {exc}") from exc
        if not isinstance(row, dict):
            raise RecipeError(f"result row is not an object at {source}:{line_number}")
        task_id = row.get("task_id")
        status = row.get("status")
        if not isinstance(task_id, str) or not task_id:
            raise RecipeError(f"result row has no task_id at {source}:{line_number}")
        if not isinstance(status, str) or not status:
            raise RecipeError(f"result row has no status at {source}:{line_number}")
        rows.append(row)
        if status in statuses:
            task_ids.append(task_id)

    summary_path = source.with_name("summary.json")
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecipeError(
            f"task-selection summary is not readable JSON: {summary_path}: {exc}"
        ) from exc
    scope = summary.get("scope") if isinstance(summary, dict) else None
    completed_tasks = scope.get("completed_tasks") if isinstance(scope, dict) else None
    if (
        isinstance(completed_tasks, bool)
        or not isinstance(completed_tasks, int)
        or completed_tasks < 0
    ):
        raise RecipeError(
            f"task-selection summary has no valid scope.completed_tasks: {summary_path}"
        )
    if completed_tasks != len(rows):
        raise RecipeError(
            "task-selection results are incomplete: "
            f"{source} has {len(rows)} row(s), but {summary_path} records "
            f"{completed_tasks} completed task(s)"
        )
    task_ids = list(dict.fromkeys(task_ids))
    if not task_ids:
        raise RecipeError(
            f"no task rows with statuses {sorted(statuses)} in {source}"
        )
    return [part for task_id in task_ids for part in ("--task", task_id)]


def render_command(
    manifest: dict[str, Any],
    recipe: dict[str, Any],
    assigned: dict[str, str],
) -> list[str]:
    values = {
        str(name): str(value)
        for name, value in (manifest.get("defaults") or {}).items()
    }
    values.update(assigned)
    unknown = sorted(set(values) - set(manifest.get("variables") or {}))
    if unknown:
        raise RecipeError("unknown variables: " + ", ".join(unknown))

    selected_tasks = selected_task_args(recipe, values)
    rendered: list[str] = []
    for raw_part in recipe["command"]:
        part = str(raw_part)
        if part == "{{selected_tasks}}":
            rendered.extend(selected_tasks)
            continue
        missing = sorted(
            name for name in PLACEHOLDER.findall(part) if not values.get(name)
        )
        if missing:
            raise RecipeError(
                "missing variables for recipe "
                f"{recipe['id']}: "
                + ", ".join(f"--set {name}=VALUE" for name in missing)
            )
        rendered.append(PLACEHOLDER.sub(lambda match: values[match.group(1)], part))
    return rendered


def validate_manifest(manifest: dict[str, Any]) -> None:
    ids = [str(recipe.get("id") or "") for recipe in manifest["recipes"]]
    if len(ids) != len(set(ids)) or any(not value for value in ids):
        raise RecipeError("recipe ids must be non-empty and unique")
    forbidden = (
        "page.capturescreenshot",
        "page.printtopdf",
        "--screenshot",
        "--pdf",
    )
    variables = set(manifest.get("variables") or {})
    defaults = manifest.get("defaults") or {}
    dynamic_manifest_value = defaults.get("dynamic_fixture_manifest")
    if "dynamic_fixture_manifest" in variables:
        if not isinstance(dynamic_manifest_value, str) or not dynamic_manifest_value:
            raise RecipeError("dynamic fixture manifest default is missing")
        dynamic_manifest_path = pathlib.Path(dynamic_manifest_value)
        if dynamic_manifest_path.is_absolute():
            raise RecipeError("dynamic fixture manifest default must be repository-relative")
        dynamic_manifest_path = (REPO_ROOT / dynamic_manifest_path).resolve()
        if not dynamic_manifest_path.is_relative_to(REPO_ROOT.resolve()):
            raise RecipeError("dynamic fixture manifest default escapes the repository")
        try:
            dynamic_payload = json.loads(
                dynamic_manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise RecipeError(
                f"dynamic fixture manifest default is unreadable: {exc}"
            ) from exc
        if (
            not isinstance(dynamic_payload, dict)
            or dynamic_payload.get("schema")
            != "experimental.kitesurf_dynamic_fixture.v1"
        ):
            raise RecipeError("dynamic fixture manifest default has an unsupported schema")
    for recipe in manifest["recipes"]:
        command = recipe.get("command")
        if not isinstance(command, list) or len(command) < 2:
            raise RecipeError(f"recipe {recipe['id']} has no executable command")
        if command[0] not in ALLOWED_LAUNCHERS:
            raise RecipeError(f"recipe {recipe['id']} uses launcher {command[0]!r}")
        entrypoint = REPO_ROOT / str(command[1])
        if not entrypoint.is_file():
            raise RecipeError(f"recipe {recipe['id']} entrypoint is missing: {entrypoint}")
        command_text = " ".join(map(str, command)).lower()
        if any(value in command_text for value in forbidden):
            raise RecipeError(f"recipe {recipe['id']} includes an excluded output task")
        referenced = {
            name
            for part in command
            for name in PLACEHOLDER.findall(str(part))
            if name != "selected_tasks"
        }
        unknown = sorted(referenced - variables)
        if unknown:
            raise RecipeError(
                f"recipe {recipe['id']} references unknown variables: {unknown}"
            )
        identity_bound = any(
            "{{kitesurf_endpoint}}" in str(part) for part in command
        )
        if recipe.get("category") == "orchestration" and not identity_bound:
            raise RecipeError(
                f"recipe {recipe['id']} omits Kitesurf endpoint variable"
            )
        required_identity = {
            "kitesurf_product",
            "kitesurf_protocol_version",
            "kitesurf_revision",
        }
        if identity_bound and not required_identity <= referenced:
            missing_identity = sorted(required_identity - referenced)
            raise RecipeError(
                f"recipe {recipe['id']} omits Kitesurf identity variable(s): "
                + ", ".join(missing_identity)
            )
        if (
            "fixture_base_url" in referenced
            and "dynamic_fixture_manifest" not in referenced
        ):
            raise RecipeError(
                f"recipe {recipe['id']} omits dynamic fixture manifest variable"
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("list", help="list versioned experiment recipes")
    subparsers.add_parser("check", help="validate the manifest and entrypoints")
    for action in ("render", "run"):
        command = subparsers.add_parser(action)
        command.add_argument("recipe")
        command.add_argument("--set", action="append", default=[], metavar="NAME=VALUE")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest = load_manifest()
    validate_manifest(manifest)
    if args.action == "list":
        for recipe in manifest["recipes"]:
            print(
                f"{recipe['id']}\t{recipe['category']}\t{recipe['description']}"
            )
        return 0
    if args.action == "check":
        print(f"ok: {len(manifest['recipes'])} recipes in {MANIFEST_PATH}")
        return 0
    recipe = recipe_by_id(manifest, args.recipe)
    command = render_command(manifest, recipe, parse_assignments(args.set))
    print(shlex.join(command), flush=True)
    if args.action == "render":
        return 0
    return subprocess.run(command, cwd=REPO_ROOT, check=False).returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RecipeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
