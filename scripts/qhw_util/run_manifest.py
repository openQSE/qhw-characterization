#!/usr/bin/env python3
"""Resolve run-all test plans from the YAML manifest."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

try:
	import yaml
except Exception as exc:  # pragma: no cover - import error path
	print(
		"ERROR: PyYAML is required to read config/qhw_tests.yaml. "
		"Install this repository's requirements.txt.",
		file=sys.stderr,
	)
	raise SystemExit(1) from exc


def load_manifest(path: Path) -> dict[str, Any]:
	with path.open("r", encoding="utf-8") as stream:
		data = yaml.safe_load(stream)
	if not isinstance(data, dict):
		raise ValueError(f"manifest must be a mapping: {path}")
	return data


def levels(manifest: dict[str, Any]) -> list[str]:
	items = manifest.get("levels", [])
	if not isinstance(items, list) or not items:
		raise ValueError("manifest must define a non-empty levels list")

	result = []
	for item in items:
		if not isinstance(item, dict) or not item.get("name"):
			raise ValueError("each level must be a mapping with a name")
		result.append(str(item["name"]))
	if len(set(result)) != len(result):
		raise ValueError("level names must be unique")
	return result


def default_level(manifest: dict[str, Any]) -> str:
	level = str(manifest.get("default-level", ""))
	if not level:
		raise ValueError("manifest must define default-level")
	if level not in levels(manifest):
		raise ValueError(f"default-level is not defined in levels: {level}")
	return level


def level_rank(manifest: dict[str, Any], level: str) -> int:
	ordered = levels(manifest)
	try:
		return ordered.index(level)
	except ValueError as exc:
		raise ValueError(
			f"unknown level {level!r}; known levels: {', '.join(ordered)}"
		) from exc


def resolve_arg(item: Any) -> str | None:
	if isinstance(item, (str, int, float)):
		return str(item)
	if not isinstance(item, dict):
		raise ValueError(f"argument entries must be scalars or mappings: {item!r}")

	env_name = item.get("env")
	default = item.get("default")
	if env_name:
		value = os.environ.get(str(env_name))
		if value is not None:
			return value
	if default is None:
		return None
	return str(default)


def resolve_args(manifest: dict[str, Any], test: dict[str, Any],
		 requested_level: str) -> list[str]:
	args_by_level = test.get("args", {})
	if args_by_level is None:
		return []
	if not isinstance(args_by_level, dict):
		raise ValueError(f"test args must be a mapping: {test.get('name')}")

	ordered = levels(manifest)
	requested_rank = level_rank(manifest, requested_level)
	selected = None
	selected_rank = -1
	for level, items in args_by_level.items():
		rank = level_rank(manifest, str(level))
		if rank <= requested_rank and rank > selected_rank:
			selected = items
			selected_rank = rank

	if selected is None:
		return []
	if not isinstance(selected, list):
		raise ValueError(f"args for test {test.get('name')} must be a list")

	result = []
	for item in selected:
		value = resolve_arg(item)
		if value is not None:
			result.append(value)
	return result


def resolve_backend_args(test: dict[str, Any],
			 backend_override: str | None = None) -> list[str]:
	config = test.get("backend", {})
	if config is None:
		config = {}
	if not isinstance(config, dict):
		raise ValueError(f"backend config must be a mapping: {test.get('name')}")

	mode = backend_override or config.get("default", "auto")
	args = ["--backend", str(mode)]

	provider = config.get("provider")
	direct_config = config.get("direct", {})
	if isinstance(direct_config, dict):
		provider = direct_config.get("provider", provider)
	if provider:
		args.extend(["--provider", str(provider)])

	qfw_config = config.get("qfw", {})
	if qfw_config is None:
		qfw_config = {}
	if not isinstance(qfw_config, dict):
		raise ValueError(f"qfw backend config must be a mapping: {test.get('name')}")
	qfw_type = qfw_config.get("type")
	if qfw_type:
		args.extend(["--qfw-type", str(qfw_type)])
	capabilities = qfw_config.get("capabilities", [])
	if isinstance(capabilities, str):
		capabilities = [capabilities]
	if capabilities is None:
		capabilities = []
	if not isinstance(capabilities, list):
		raise ValueError(
			f"qfw capabilities must be a list: {test.get('name')}")
	for capability in capabilities:
		args.extend(["--qfw-capability", str(capability)])

	return args


def selected_tests(manifest: dict[str, Any], requested_level: str) -> list[dict[str, Any]]:
	requested_rank = level_rank(manifest, requested_level)
	tests = manifest.get("tests", [])
	if not isinstance(tests, list):
		raise ValueError("manifest tests must be a list")

	selected = []
	for test in tests:
		if not isinstance(test, dict):
			raise ValueError(f"test entries must be mappings: {test!r}")
		name = test.get("name")
		script = test.get("script")
		level = test.get("level")
		if not name or not script or not level:
			raise ValueError("each test must define name, level, and script")
		if level_rank(manifest, str(level)) <= requested_rank:
			selected.append(test)
	return selected


def emit_plan(manifest: dict[str, Any], requested_level: str,
	      backend_override: str | None = None) -> None:
	for test in selected_tests(manifest, requested_level):
		fields = [str(test["script"])]
		fields.extend(resolve_backend_args(test, backend_override))
		fields.extend(resolve_args(manifest, test, requested_level))
		if any("\t" in field or "\n" in field for field in fields):
			raise ValueError("manifest values must not contain tabs or newlines")
		print("\t".join(fields))


def emit_levels(manifest: dict[str, Any]) -> None:
	for item in manifest["levels"]:
		name = str(item["name"])
		description = str(item.get("description", ""))
		print(f"{name}\t{description}")


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser()
	parser.add_argument("--manifest", type=Path, required=True)
	subparsers = parser.add_subparsers(dest="command", required=True)

	subparsers.add_parser("default-level")
	subparsers.add_parser("levels")

	plan = subparsers.add_parser("plan")
	plan.add_argument("--level", required=True)
	plan.add_argument("--backend", default=None)
	return parser.parse_args()


def main() -> int:
	args = parse_args()
	manifest = load_manifest(args.manifest)
	if args.command == "default-level":
		print(default_level(manifest))
	elif args.command == "levels":
		emit_levels(manifest)
	elif args.command == "plan":
		emit_plan(manifest, args.level, args.backend)
	else:
		raise ValueError(f"unsupported command: {args.command}")
	return 0


if __name__ == "__main__":
	try:
		raise SystemExit(main())
	except Exception as exc:
		print(f"ERROR: {exc}", file=sys.stderr)
		raise SystemExit(1) from exc
