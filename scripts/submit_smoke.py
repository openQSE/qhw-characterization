#!/usr/bin/env python3
"""Submit a one-qubit Qiskit smoke circuit through the selected backend."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from qhw_util.workflow import WorkflowContext


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


def add_script_args(parser: argparse.ArgumentParser) -> None:
	parser.add_argument("--shots", type=int, default=100)
	parser.add_argument("--flip", action="store_true")


def main() -> int:
	ctx = WorkflowContext.from_cli(
		__file__,
		description="Run a minimal hardware circuit authored with Qiskit.",
		add_args=add_script_args,
		calibration=True,
		execution=True,
	)
	args = ctx.args

	circuit = build_smoke_circuit(args.flip)

	run = ctx.run_circuit(
		circuit,
		name="result",
		qasm_name="smoke",
		shots=args.shots,
	)

	input_file = ctx.write_input({
		"source": "qiskit",
		"shots": args.shots,
		"flip": args.flip,
		"calibration_set_id": args.calibration_set_id,
		"use_timeslot": args.use_timeslot,
		"qasm_artifact": run.files.get("qasm"),
	})
	timing_file = ctx.write_json(
		ctx.paths.results / "timing_summary.json", run.timing)

	summary = {
		"ok": run.ok,
		"backend_mode": ctx.backend_name,
		"job_id": run.job_id,
		"counts": run.counts,
		"files": {
			"input": str(input_file),
			**run.files,
			"timing_summary": str(timing_file),
			"script_output": str(ctx.script_output_file),
		},
	}
	lines = [
		f"run id: {ctx.paths.run_id}",
		f"output dir: {ctx.paths.root}",
		f"job id: {summary['job_id']}",
		f"counts: {summary['counts']}",
	]
	for name, path in summary["files"].items():
		lines.append(f"{name}: {path}")
	return ctx.finish(summary, ok=run.ok, text_lines=lines)


if __name__ == "__main__":
	raise SystemExit(main())
