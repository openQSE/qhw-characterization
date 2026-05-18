#!/usr/bin/env python3
"""Submit a one-qubit Qiskit smoke circuit through the selected backend."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from qhw_util.args import add_common_arguments
from qhw_util.backend import get_backend_from_args
from qhw_util.output import backend_result_qhw
from qhw_util.output import create_run_paths
from qhw_util.output import render_json_output
from qhw_util.output import render_text_output
from qhw_util.output import script_output_path
from qhw_util.output import to_jsonable
from qhw_util.output import write_backend_result_artifacts
from qhw_util.output import write_json
from qhw_util.output import write_script_output
from qhw_util.qiskit_exec import write_qasm2_artifact


def build_smoke_circuit(flip: bool):
	try:
		from qiskit import QuantumCircuit
	except Exception as exc:
		raise RuntimeError(
			"qiskit is required for submit_smoke.py") from exc
	circuit = QuantumCircuit(1, 1, name="submit_smoke")
	if flip:
		circuit.x(0)
	circuit.measure(0, 0)
	return circuit


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(
		description="Run a minimal hardware circuit authored with Qiskit.",
	)
	parser.add_argument("--shots", type=int, default=100)
	parser.add_argument("--flip", action="store_true")
	add_common_arguments(parser, calibration=True, execution=True)
	return parser.parse_args()


def main() -> int:
	args = parse_args()
	paths = create_run_paths(__file__, args.output_dir, args.run_id)
	backend = get_backend_from_args(args)

	circuit = build_smoke_circuit(args.flip)
	qasm_file = paths.circuits / "smoke.qasm"
	input_file = paths.root / "input.json"
	result_file = paths.results / "result.json"
	timing_file = paths.results / "timing_summary.json"

	write_qasm2_artifact(circuit, qasm_file)
	write_json(input_file, {
		"source": "qiskit",
		"shots": args.shots,
		"flip": args.flip,
		"calibration_set_id": args.calibration_set_id,
		"use_timeslot": args.use_timeslot,
		"qasm_artifact": str(qasm_file),
	})

	job = backend.run(
		[circuit],
		shots=args.shots,
		calibration_set_id=args.calibration_set_id,
		timeout=args.timeout,
		use_timeslot=args.use_timeslot)
	result = to_jsonable(job.result(timeout=args.timeout))
	result_files = write_backend_result_artifacts(result_file, result)

	qhw_result = backend_result_qhw(result)
	if not qhw_result:
		raise ValueError("backend result did not include normalized qhw_result")
	qhw_payload = qhw_result.get("result", {})
	qhw_job = qhw_result.get("job", {})
	timing_summary = qhw_result.get("timing", {})
	write_json(timing_file, timing_summary or {})

	summary = {
		"ok": result.get("rc") == 0,
		"run_id": paths.run_id,
		"date_id": paths.date_id,
		"backend_mode": backend.name,
		"output_dir": str(paths.root),
		"job_id": qhw_job.get("id") or result.get("cid"),
		"counts": qhw_payload.get("counts")
		if isinstance(qhw_payload, dict) else None,
		"files": {
			"input": str(input_file),
			"qasm": str(qasm_file),
			"result": result_files.get("qhw"),
			"raw_result": result_files.get("raw"),
			"normalized_result": result_files.get("qhw"),
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
			f"counts: {summary['counts']}",
		]
		for name, path in summary["files"].items():
			lines.append(f"{name}: {path}")
		output = render_text_output(lines)
	write_script_output(paths, output, args.json)

	return backend.finish(0 if summary["ok"] else 2)


if __name__ == "__main__":
	raise SystemExit(main())
