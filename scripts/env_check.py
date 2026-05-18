#!/usr/bin/env python3
"""Validate access to a hardware backend."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from qhw_util.args import add_common_arguments
from qhw_util.backend import get_backend_from_args
from qhw_util.output import create_run_paths
from qhw_util.output import render_json_output
from qhw_util.output import render_text_output
from qhw_util.output import script_output_path
from qhw_util.output import to_jsonable
from qhw_util.output import write_json
from qhw_util.output import write_script_output
from qhw_util.schema import qhw_device_summary


def summarize_backend(info: dict[str, Any],
		      device_info: dict[str, Any]) -> dict[str, Any]:
	device_summary = qhw_device_summary(device_info)
	summary = {
		"backend": info.get("backend"),
		"metadata_supported": info.get("metadata_supported"),
		"name": device_summary.get("name"),
		"active_qubits": device_summary.get("active_qubits", []),
		"calibration_set_id": device_summary.get("calibration_set_id"),
		"device": device_summary,
	}
	if not summary["name"]:
		static_arch = info.get("static_architecture", {})
		summary["name"] = static_arch.get("name") or static_arch.get(
			"dut_label")
	return summary


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(
		description="Check that the test workflow can reach a backend.",
	)
	add_common_arguments(parser, calibration=True)
	return parser.parse_args()


def main() -> int:
	args = parse_args()
	paths = create_run_paths(__file__, args.output_dir, args.run_id)
	backend = get_backend_from_args(args)

	result = {
		"ok": True,
		"backend_mode": backend.name,
		"backend_info": to_jsonable(backend.get_backend_info()),
		"device_info": to_jsonable(backend.get_device_info()),
		"dynamic_backend_info": to_jsonable(
			backend.get_dynamic_backend_info(args.calibration_set_id)),
	}
	result["summary"] = summarize_backend(
		result["backend_info"], result["device_info"])

	output_file = paths.root / "env_check.json"
	write_json(output_file, result)
	result["output_file"] = str(output_file)
	result["script_output_file"] = str(script_output_path(paths, args.json))

	if args.json:
		output = render_json_output(result)
	else:
		summary = result["summary"]
		output = render_text_output([
			f"backend: {summary['backend']}",
			f"machine: {summary['name']}",
			f"active qubits: {len(summary['active_qubits'])}",
			f"calibration set: {summary['calibration_set_id']}",
			f"output: {output_file}",
		])
	write_script_output(paths, output, args.json)

	return backend.finish(0)


if __name__ == "__main__":
	raise SystemExit(main())
