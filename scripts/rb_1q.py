#!/usr/bin/env python3
"""Run single-qubit randomized benchmarking style circuits."""

from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from qhw_util.experiments import dry_run_result
from qhw_util.experiments import exponential_decay_fit
from qhw_util.experiments import parse_int_list
from qhw_util.experiments import resolve_qubits
from qhw_util.experiments import success_probability
from qhw_util.experiments import write_jsonl
from qhw_util.output import qhw_json_path
from qhw_util.output import to_jsonable
from qhw_util.schema import qhw_device_qubits
from qhw_util.workflow import WorkflowContext


def dry_run_counts(length: int, shots: int, error_per_clifford: float) -> dict[str, int]:
	survival = 0.5 + 0.5 * ((1.0 - error_per_clifford) ** length)
	zeros = int(round(shots * survival))
	ones = shots - zeros
	return {"0": zeros, "1": ones} if ones else {"0": zeros}


def build_rb_circuit(length: int, seed: int, name: str):
	try:
		from qiskit import QuantumCircuit
		from qiskit.quantum_info import Clifford
		from qiskit.quantum_info import random_clifford
	except Exception as exc:
		raise RuntimeError("qiskit is required for rb_1q.py") from exc

	body = QuantumCircuit(1, name=f"{name}_body")
	for index in range(length):
		clifford = random_clifford(1, seed=seed + index)
		body.compose(clifford.to_circuit(), inplace=True)
	inverse = Clifford(body).adjoint().to_circuit()

	circuit = QuantumCircuit(1, 1, name=name)
	circuit.compose(body, inplace=True)
	circuit.compose(inverse, inplace=True)
	circuit.measure(0, 0)
	return circuit


def run_record(ctx: WorkflowContext, *, qubit: str, length: int,
	       sequence_index: int, shots: int, seed: int,
	       dry_run_error_per_clifford: float) -> dict[str, Any]:
	cid = f"rb_1q_{qubit}_l{length}_seq{sequence_index}_s{shots}"
	circuit = build_rb_circuit(length, seed, cid)
	mapping = {0: qubit}
	start = time.monotonic()
	try:
		if ctx.args.dry_run:
			qasm_files = ctx.write_qasm_artifacts(circuit, cid)
			result = dry_run_result(
				cid,
				shots,
				dry_run_counts(length, shots, dry_run_error_per_clifford),
				execution_seconds=shots * (0.0004 + length * 0.00002))
			run = ctx.write_backend_result(cid, result, qasm_files)
		else:
			run = ctx.run_circuit(
				circuit,
				name=cid,
				qasm_name=cid,
				shots=shots,
				qubit_mapping=mapping)
		script_wall = time.monotonic() - start
		return {
			"ok": run.ok,
			"physical_qubit": qubit,
			"length": length,
			"sequence_index": sequence_index,
			"seed": seed,
			"shots": shots,
			"job_id": run.job_id,
			"counts": run.counts or {},
			"metrics": {
				"script_wall_seconds": script_wall,
				"survival_probability": success_probability(
					run.counts or {}, 1, [0]),
			},
			"files": run.files,
		}
	except Exception as exc:
		return {
			"ok": False,
			"physical_qubit": qubit,
			"length": length,
			"sequence_index": sequence_index,
			"seed": seed,
			"shots": shots,
			"error": str(exc),
			"counts": {},
			"metrics": {},
			"files": {},
		}


def build_analysis(records: list[dict[str, Any]],
		   config: dict[str, Any]) -> dict[str, Any]:
	by_qubit: dict[str, dict[int, list[float]]] = {}
	for record in records:
		if not record.get("ok"):
			continue
		value = record.get("metrics", {}).get("survival_probability")
		if value is None:
			continue
		by_qubit.setdefault(record["physical_qubit"], {}).setdefault(
			int(record["length"]), []).append(float(value))

	fits = {}
	for qubit, lengths in sorted(by_qubit.items()):
		points = []
		for length, values in sorted(lengths.items()):
			points.append((float(length), sum(values) / len(values)))
		contrast_points = [
			(length, max(value - 0.5, 0.0))
			for length, value in points
		]
		fit = exponential_decay_fit(contrast_points, floor=0.0)
		fits[qubit] = {
			"points": [
				{"length": length, "survival_probability": value}
				for length, value in points
			],
			"decay_fit": fit,
			"estimated_error_per_clifford": (
				1.0 / fit["decay_constant"]
				if fit and fit.get("decay_constant") else None),
		}

	return {
		"schema": "qhw-rb-1q-analysis-v1",
		"intent": (
			"Estimate single-qubit RB survival decay versus Clifford "
			"sequence length."),
		"config": config,
		"record_count": len(records),
		"successful_record_count": sum(
			1 for record in records if record.get("ok")),
		"failed_record_count": sum(
			1 for record in records if not record.get("ok")),
		"fits": fits,
		"caveats": [
			"The circuits are Qiskit Clifford-authored RB sequences. "
			"Provider transpilation may change the native gate expansion.",
			"The reported error per Clifford is a simple decay-derived "
			"summary, not a full RB confidence interval.",
		],
	}


def render_analysis_markdown(analysis: dict[str, Any]) -> str:
	lines = [
		"# 1Q Randomized Benchmarking Analysis",
		"",
		analysis["intent"],
		"",
		"| Qubit | Estimated error per Clifford | Points |",
		"| --- | ---: | ---: |",
	]
	for qubit, item in analysis["fits"].items():
		lines.append(
			f"| `{qubit}` | {item['estimated_error_per_clifford']} | "
			f"{len(item['points'])} |")
	lines += ["", "## Caveats", ""]
	for caveat in analysis["caveats"]:
		lines.append(f"- {caveat}")
	return "\n".join(lines) + "\n"


def add_script_args(parser: argparse.ArgumentParser) -> None:
	parser.add_argument("--qubits", default="all")
	parser.add_argument("--lengths", type=parse_int_list,
			    default=parse_int_list("1,2,4,8,16,32"))
	parser.add_argument("--sequences", type=int, default=8)
	parser.add_argument("--shots", type=int, default=1000)
	parser.add_argument("--sample-seed", type=int, default=11)
	parser.add_argument("--dry-run-error-per-clifford", type=float, default=0.004)


def main() -> int:
	ctx = WorkflowContext.from_cli(
		__file__,
		description="Run single-qubit randomized benchmarking circuits.",
		add_args=add_script_args,
		calibration=True,
		execution=True,
		dry_run=True,
	)
	args = ctx.args
	if args.shots < 1:
		raise ValueError("--shots must be at least 1")
	if args.sequences < 1:
		raise ValueError("--sequences must be at least 1")

	backend_info = {} if args.dry_run else to_jsonable(
		ctx.backend.get_backend_info())
	device_info = {} if args.dry_run else to_jsonable(
		ctx.backend.get_device_info())
	qubits = resolve_qubits(
		args.qubits, qhw_device_qubits(device_info), args.dry_run)

	backend_info_file = ctx.paths.root / "backend_info.json"
	device_info_file = qhw_json_path(ctx.paths.root, "device_info")
	records_file = ctx.paths.results / "rb_1q_records.jsonl"
	analysis_file = ctx.paths.results / "analysis.json"
	analysis_md_file = ctx.paths.results / "analysis.md"
	summary_file = ctx.paths.results / "rb_1q_summary.json"
	ctx.write_json(backend_info_file, backend_info)
	ctx.write_json(device_info_file, device_info)

	rng = random.Random(args.sample_seed)
	records = []
	for qubit in qubits:
		for length in args.lengths:
			for sequence_index in range(args.sequences):
				seed = rng.randrange(1, 2**31)
				records.append(run_record(
					ctx,
					qubit=qubit,
					length=int(length),
					sequence_index=sequence_index,
					shots=args.shots,
					seed=seed,
					dry_run_error_per_clifford=(
						args.dry_run_error_per_clifford)))

	write_jsonl(records_file, records)
	config = {
		"backend": ctx.backend_name,
		"qubits": qubits,
		"lengths": args.lengths,
		"sequences": args.sequences,
		"shots": args.shots,
		"sample_seed": args.sample_seed,
		"dry_run": args.dry_run,
	}
	analysis = build_analysis(records, config)
	ctx.write_json(analysis_file, analysis)
	analysis_md_file.write_text(render_analysis_markdown(analysis))
	summary = {
		"ok": not any(not record.get("ok") for record in records),
		"backend_mode": ctx.backend_name,
		"records": len(records),
		"successful_records": sum(1 for record in records if record.get("ok")),
		"failed_records": sum(1 for record in records if not record.get("ok")),
		"files": {
			"backend_info": str(backend_info_file),
			"device_info": str(device_info_file),
			"records": str(records_file),
			"analysis_json": str(analysis_file),
			"analysis_markdown": str(analysis_md_file),
			"summary": str(summary_file),
		},
	}
	ctx.write_json(summary_file, summary)
	return ctx.finish(
		summary,
		ok=summary["ok"],
		text_lines=[
			f"records: {summary['records']}",
			f"successful records: {summary['successful_records']}",
			f"output: {ctx.paths.root}",
		],
	)


if __name__ == "__main__":
	raise SystemExit(main())
