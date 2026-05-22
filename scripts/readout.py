#!/usr/bin/env python3
"""Characterize measurement/readout behavior with Qiskit circuits."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from qhw_util.experiments import counts_total
from qhw_util.experiments import dry_run_result
from qhw_util.experiments import hamming_error_probability
from qhw_util.experiments import independent_readout_counts
from qhw_util.experiments import logical_one_probability
from qhw_util.experiments import logical_bits_to_count_key
from qhw_util.experiments import mean_or_none
from qhw_util.experiments import parse_int_list
from qhw_util.experiments import resolve_qubits
from qhw_util.experiments import resolve_widths
from qhw_util.experiments import success_probability
from qhw_util.experiments import write_jsonl
from qhw_util.output import backend_result_qhw
from qhw_util.output import qhw_json_path
from qhw_util.output import to_jsonable
from qhw_util.schema import qhw_device_qubits
from qhw_util.workflow import WorkflowContext


def build_basis_circuit(bits: list[int], name: str):
	try:
		from qiskit import QuantumCircuit
	except Exception as exc:
		raise RuntimeError("qiskit is required for readout.py") from exc
	width = len(bits)
	circuit = QuantumCircuit(width, width, name=name)
	for index, bit in enumerate(bits):
		if bit:
			circuit.x(index)
	circuit.measure(range(width), range(width))
	return circuit


def bit_patterns(width: int) -> list[list[int]]:
	return [
		[(value >> index) & 1 for index in range(width)]
		for value in range(1 << width)
	]


def extract_counts(result: dict[str, Any]) -> dict[str, int]:
	qhw_result = backend_result_qhw(result)
	payload = qhw_result.get("result", {}) if qhw_result else {}
	counts = payload.get("counts") if isinstance(payload, dict) else {}
	if not isinstance(counts, dict):
		return {}
	return {str(key): int(value) for key, value in counts.items()}


def run_basis_record(ctx: WorkflowContext, *, cid: str,
		     experiment: str, bits: list[int],
		     physical_qubits: list[str], shots: int,
		     repetition: int, dry_run_error: float,
		     dry_run_correlation: float = 0.0) -> dict[str, Any]:
	circuit = build_basis_circuit(bits, cid)
	mapping = {
		index: qubit
		for index, qubit in enumerate(physical_qubits)
	}
	start = time.monotonic()
	try:
		if ctx.args.dry_run:
			qasm_files = ctx.write_qasm_artifacts(circuit, cid)
			counts = independent_readout_counts(
				bits, shots, dry_run_error, dry_run_correlation)
			result = dry_run_result(
				cid,
				shots,
				counts,
				execution_seconds=shots * (0.0005 + 0.00002 * len(bits)))
			run = ctx.write_backend_result(cid, result, qasm_files)
		else:
			run = ctx.run_circuit(
				circuit,
				name=cid,
				qasm_name=cid,
				shots=shots,
				qubit_mapping=mapping)
		script_wall = time.monotonic() - start
		counts = extract_counts(run.result)
		record = {
			"ok": run.ok,
			"experiment": experiment,
			"circuit_id": cid,
			"repetition": repetition,
			"width": len(bits),
			"physical_qubits": physical_qubits,
			"expected_bits": bits,
			"expected_count_key": logical_bits_to_count_key(bits),
			"shots": shots,
			"qubit_mapping": {
				str(key): value for key, value in mapping.items()
			},
			"job_id": run.job_id,
			"counts": counts,
			"metrics": {
				"script_wall_seconds": script_wall,
				"success_probability": success_probability(
					counts, len(bits), bits),
				"hamming_error_probability": hamming_error_probability(
					counts, len(bits), bits),
			},
			"files": run.files,
		}
	except Exception as exc:
		record = {
			"ok": False,
			"experiment": experiment,
			"circuit_id": cid,
			"repetition": repetition,
			"width": len(bits),
			"physical_qubits": physical_qubits,
			"expected_bits": bits,
			"expected_count_key": logical_bits_to_count_key(bits),
			"shots": shots,
			"qubit_mapping": {
				str(key): value for key, value in mapping.items()
			},
			"error": str(exc),
			"counts": {},
			"metrics": {},
			"files": {},
		}
	return record


def per_qubit_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
	by_qubit: dict[str, dict[int, list[dict[str, Any]]]] = {}
	for record in records:
		if record.get("experiment") != "per_qubit_readout" or not record.get("ok"):
			continue
		qubit = record["physical_qubits"][0]
		prepared = int(record["expected_bits"][0])
		by_qubit.setdefault(qubit, {}).setdefault(prepared, []).append(record)

	summary = {}
	for qubit, states in sorted(by_qubit.items()):
		p_one_given_zero = []
		p_zero_given_one = []
		for record in states.get(0, []):
			p_one = logical_one_probability(record["counts"], 1, 0)
			if p_one is not None:
				p_one_given_zero.append(p_one)
		for record in states.get(1, []):
			p_one = logical_one_probability(record["counts"], 1, 0)
			if p_one is not None:
				p_zero_given_one.append(1.0 - p_one)
		err_0 = mean_or_none(p_one_given_zero)
		err_1 = mean_or_none(p_zero_given_one)
		avg_error = mean_or_none([err_0, err_1])
		summary[qubit] = {
			"p_measured_1_given_prepared_0": err_0,
			"p_measured_0_given_prepared_1": err_1,
			"average_assignment_error": avg_error,
			"assignment_fidelity": (
				1.0 - avg_error if avg_error is not None else None),
			"records": len(states.get(0, [])) + len(states.get(1, [])),
		}
	return summary


def assignment_matrices(records: list[dict[str, Any]]) -> dict[str, Any]:
	matrices: dict[str, dict[str, Any]] = {}
	for record in records:
		if record.get("experiment") != "assignment_matrix" or not record.get("ok"):
			continue
		width = int(record["width"])
		key = ",".join(record["physical_qubits"])
		matrix = matrices.setdefault(key, {
			"width": width,
			"physical_qubits": record["physical_qubits"],
			"rows": {},
		})
		total = counts_total(record["counts"])
		row = {}
		if total:
			for count_key, value in record["counts"].items():
				row[count_key] = int(value) / total
		matrix["rows"][record["expected_count_key"]] = row
	return matrices


def correlation_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
	items = []
	for record in records:
		if record.get("experiment") != "readout_correlation" or not record.get("ok"):
			continue
		items.append({
			"physical_qubits": record["physical_qubits"],
			"expected_count_key": record["expected_count_key"],
			"width": record["width"],
			"success_probability": (
				record.get("metrics", {}).get("success_probability")),
			"hamming_error_probability": (
				record.get("metrics", {}).get("hamming_error_probability")),
		})
	return {
		"records": len(items),
		"mean_success_probability": mean_or_none([
			item["success_probability"] for item in items]),
		"mean_hamming_error_probability": mean_or_none([
			item["hamming_error_probability"] for item in items]),
		"items": items,
	}


def build_analysis(records: list[dict[str, Any]],
		   config: dict[str, Any]) -> dict[str, Any]:
	per_qubit = per_qubit_summary(records)
	assignment = assignment_matrices(records)
	correlation = correlation_summary(records)
	errors = [
		item["average_assignment_error"]
		for item in per_qubit.values()
		if item["average_assignment_error"] is not None
	]
	return {
		"schema": "qhw-readout-analysis-v1",
		"intent": (
			"Estimate per-qubit assignment error, small-subset assignment "
			"matrices, and multi-qubit readout scaling."),
		"config": config,
		"record_count": len(records),
		"successful_record_count": sum(
			1 for record in records if record.get("ok")),
		"failed_record_count": sum(
			1 for record in records if not record.get("ok")),
		"overall": {
			"mean_assignment_error": mean_or_none(errors),
			"mean_assignment_fidelity": (
				1.0 - mean_or_none(errors) if errors else None),
		},
		"per_qubit": per_qubit,
		"assignment_matrices": assignment,
		"correlation": correlation,
		"caveats": [
			"Counts use Qiskit bitstring ordering; logical qubit 0 maps to "
			"the right-most count bit.",
			"Assignment matrices are only generated for the configured small "
			"subsets to avoid exponential growth.",
		],
	}


def render_analysis_markdown(analysis: dict[str, Any]) -> str:
	lines = [
		"# Readout Analysis",
		"",
		analysis["intent"],
		"",
		"## Summary",
		"",
		f"Records: {analysis['record_count']}",
		f"Successful records: {analysis['successful_record_count']}",
		f"Failed records: {analysis['failed_record_count']}",
		f"Mean assignment error: "
		f"{analysis['overall']['mean_assignment_error']}",
		f"Mean assignment fidelity: "
		f"{analysis['overall']['mean_assignment_fidelity']}",
		"",
		"## Per-Qubit Assignment",
		"",
		"| Qubit | P(1|0) | P(0|1) | Average error | Fidelity |",
		"| --- | ---: | ---: | ---: | ---: |",
	]
	for qubit, item in analysis["per_qubit"].items():
		lines.append(
			f"| `{qubit}` | "
			f"{item['p_measured_1_given_prepared_0']} | "
			f"{item['p_measured_0_given_prepared_1']} | "
			f"{item['average_assignment_error']} | "
			f"{item['assignment_fidelity']} |")
	lines += [
		"",
		"## Readout Scaling",
		"",
		f"Correlation records: {analysis['correlation']['records']}",
		f"Mean success probability: "
		f"{analysis['correlation']['mean_success_probability']}",
		f"Mean hamming error probability: "
		f"{analysis['correlation']['mean_hamming_error_probability']}",
		"",
		"## Caveats",
		"",
	]
	for caveat in analysis["caveats"]:
		lines.append(f"- {caveat}")
	return "\n".join(lines) + "\n"


def add_script_args(parser: argparse.ArgumentParser) -> None:
	parser.add_argument("--qubits", default="all")
	parser.add_argument("--widths", default="1,2,4,max")
	parser.add_argument("--assignment-widths", type=parse_int_list,
			    default=parse_int_list("1,2"))
	parser.add_argument("--max-assignment-width", type=int, default=3)
	parser.add_argument("--shots", type=int, default=1000)
	parser.add_argument("--repetitions", type=int, default=1)
	parser.add_argument("--dry-run-error", type=float, default=0.02)
	parser.add_argument("--dry-run-correlation", type=float, default=0.002)


def main() -> int:
	ctx = WorkflowContext.from_cli(
		__file__,
		description="Characterize readout error and readout scaling.",
		add_args=add_script_args,
		calibration=True,
		execution=True,
		dry_run=True,
	)
	args = ctx.args
	if args.shots < 1:
		raise ValueError("--shots must be at least 1")
	if args.repetitions < 1:
		raise ValueError("--repetitions must be at least 1")

	backend_info = {} if args.dry_run else to_jsonable(
		ctx.backend.get_backend_info())
	device_info = {} if args.dry_run else to_jsonable(
		ctx.backend.get_device_info())
	active_qubits = qhw_device_qubits(device_info)
	qubits = resolve_qubits(args.qubits, active_qubits, args.dry_run)
	widths = resolve_widths(args.widths, qubits)

	backend_info_file = ctx.paths.root / "backend_info.json"
	device_info_file = qhw_json_path(ctx.paths.root, "device_info")
	records_file = ctx.paths.results / "readout_records.jsonl"
	analysis_file = ctx.paths.results / "analysis.json"
	analysis_md_file = ctx.paths.results / "analysis.md"
	summary_file = ctx.paths.results / "readout_summary.json"
	ctx.write_json(backend_info_file, backend_info)
	ctx.write_json(device_info_file, device_info)

	records = []
	for repetition in range(args.repetitions):
		for qubit in qubits:
			for bit in (0, 1):
				cid = (
					f"readout_1q_{qubit}_prep{bit}_"
					f"s{args.shots}_r{repetition}")
				records.append(run_basis_record(
					ctx,
					cid=cid,
					experiment="per_qubit_readout",
					bits=[bit],
					physical_qubits=[qubit],
					shots=args.shots,
					repetition=repetition,
					dry_run_error=args.dry_run_error))

		for width in widths:
			physical = qubits[:width]
			for bit in (0, 1):
				bits = [bit] * width
				cid = (
					f"readout_corr_w{width}_prep{bit}_"
					f"s{args.shots}_r{repetition}")
				records.append(run_basis_record(
					ctx,
					cid=cid,
					experiment="readout_correlation",
					bits=bits,
					physical_qubits=physical,
					shots=args.shots,
					repetition=repetition,
					dry_run_error=args.dry_run_error,
					dry_run_correlation=args.dry_run_correlation))

		for width in args.assignment_widths:
			if width > args.max_assignment_width or width > len(qubits):
				continue
			physical = qubits[:width]
			for bits in bit_patterns(width):
				key = logical_bits_to_count_key(bits)
				cid = (
					f"readout_assign_w{width}_{key}_"
					f"s{args.shots}_r{repetition}")
				records.append(run_basis_record(
					ctx,
					cid=cid,
					experiment="assignment_matrix",
					bits=bits,
					physical_qubits=physical,
					shots=args.shots,
					repetition=repetition,
					dry_run_error=args.dry_run_error))

	write_jsonl(records_file, records)
	config = {
		"backend": ctx.backend_name,
		"qubits": qubits,
		"widths": widths,
		"assignment_widths": args.assignment_widths,
		"max_assignment_width": args.max_assignment_width,
		"shots": args.shots,
		"repetitions": args.repetitions,
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
		"analysis": {
			"mean_assignment_error": (
				analysis["overall"]["mean_assignment_error"]),
			"mean_assignment_fidelity": (
				analysis["overall"]["mean_assignment_fidelity"]),
		},
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
			f"mean assignment error: "
			f"{summary['analysis']['mean_assignment_error']}",
			f"output: {ctx.paths.root}",
		],
	)


if __name__ == "__main__":
	raise SystemExit(main())
