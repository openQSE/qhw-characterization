#!/usr/bin/env python3
"""Submit a one-qubit smoke circuit through the selected IQM backend."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qhw_util.args import add_common_arguments
from qhw_util.backend import get_backend_from_args
from qhw_util.output import backend_result_qhw
from qhw_util.output import create_run_paths
from qhw_util.output import render_json_output
from qhw_util.output import render_text_output
from qhw_util.output import script_output_path
from qhw_util.output import to_jsonable
from qhw_util.output import write_json
from qhw_util.output import write_script_output


def build_smoke_qasm(flip: bool) -> str:
	gate = "x q[0];\n" if flip else ""
	return (
		"OPENQASM 2.0;\n"
		"include \"qelib1.inc\";\n"
		"qreg q[1];\n"
		"creg c[1];\n"
		f"{gate}"
		"measure q[0] -> c[0];\n"
	)


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(
		description="Run a minimal IQM-native OpenQASM circuit.",
	)
	parser.add_argument("--shots", type=int, default=100)
	parser.add_argument("--qubit", default=None)
	parser.add_argument("--flip", action="store_true")
	add_common_arguments(parser, calibration=True, execution=True)
	return parser.parse_args()


def main() -> int:
	args = parse_args()
	paths = create_run_paths(__file__, args.output_dir, args.run_id)
	backend = get_backend_from_args(args)

	qasm = build_smoke_qasm(args.flip)
	info = {
		"qasm": qasm,
		"num_qubits": 1,
		"num_shots": args.shots,
		"compiler": "iqm-native",
		"timeout": args.timeout,
		"use_timeslot": args.use_timeslot,
	}
	if args.qubit:
		info["iqm_qubit_mapping"] = {0: args.qubit}
	if args.calibration_set_id:
		info["calibration_set_id"] = args.calibration_set_id

	qasm_file = paths.circuits / "smoke.qasm"
	input_file = paths.root / "input.json"
	result_file = paths.results / "result.json"
	timing_file = paths.results / "timing_summary.json"

	qasm_file.write_text(qasm)
	write_json(input_file, info)

	result = to_jsonable(backend.sync_run(info))
	write_json(result_file, result)

	payload = result.get("result", {})
	iqm_payload = payload.get("iqm", {}) if isinstance(payload, dict) else {}
	qhw_result = backend_result_qhw(result)
	timing_summary = qhw_result.get("timing", {}) if qhw_result else {}
	write_json(timing_file, timing_summary)

	summary = {
		"ok": result.get("rc") == 0,
		"run_id": paths.run_id,
		"date_id": paths.date_id,
		"backend_mode": backend.name,
		"output_dir": str(paths.root),
		"job_id": iqm_payload.get("job_id"),
		"status": iqm_payload.get("status"),
		"counts": payload.get("counts") if isinstance(payload, dict) else None,
		"files": {
			"input": str(input_file),
			"qasm": str(qasm_file),
			"result": str(result_file),
			"timing_summary": str(timing_file),
		},
	}
	summary["files"]["script_output"] = str(
		script_output_path(paths, args.json))

	if args.json:
		output = render_json_output(summary)
	else:
		lines = [
			f"run id: {summary['run_id']}",
			f"output dir: {summary['output_dir']}",
			f"job id: {summary['job_id']}",
			f"status: {summary['status']}",
			f"counts: {summary['counts']}",
		]
		for name, path in summary["files"].items():
			lines.append(f"{name}: {path}")
		output = render_text_output(lines)
	write_script_output(paths, output, args.json)

	return backend.finish(0 if summary["ok"] else 2)


if __name__ == "__main__":
	raise SystemExit(main())
