#!/usr/bin/env python3
"""Run a configurable surface-code memory-style QEC experiment."""

from __future__ import annotations

import argparse
import math
import random
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from qhw_util.experiments import dry_run_result
from qhw_util.experiments import normalize_count_key
from qhw_util.experiments import parse_int_list
from qhw_util.experiments import write_jsonl
from qhw_util.output import qhw_json_path
from qhw_util.output import to_jsonable
from qhw_util.schema import qhw_coupling_edges
from qhw_util.schema import qhw_coupling_nodes
from qhw_util.schema import qhw_device_qubits
from qhw_util.workflow import WorkflowContext


BASIS_CHOICES = ("z", "x", "both")
DECODER_CHOICES = ("simple", "none", "pymatching")
RESET_MODE_CHOICES = ("hardware", "none")


def parse_basis(value: str) -> list[str]:
	value = value.strip().lower()
	if value == "both":
		return ["z", "x"]
	if value in {"z", "x"}:
		return [value]
	raise argparse.ArgumentTypeError(
		f"--basis must be one of {', '.join(BASIS_CHOICES)}")


def parse_patch(value: str) -> list[str] | None:
	value = value.strip()
	if not value or value.lower() == "auto":
		return None
	qubits = [item.strip() for item in value.split(",") if item.strip()]
	if not qubits:
		raise argparse.ArgumentTypeError("--patch must be auto or a qubit list")
	return qubits


def required_qubits(distance: int) -> dict[str, int]:
	data = distance * distance
	checks = data - 1
	return {
		"distance": distance,
		"data_qubits": data,
		"check_qubits": checks,
		"total_qubits": data + checks,
	}


def synthetic_coupling_graph(total_nodes: int = 20) -> dict[str, Any]:
	nodes = [f"QB{index}" for index in range(1, total_nodes + 1)]
	edges = [[nodes[index], nodes[index + 1]]
		 for index in range(len(nodes) - 1)]
	return {
		"schema": "qhw-coupling-v1",
		"provider": "dry-run",
		"device": {"id": "dry-run", "provider": "dry-run"},
		"coupling": {"directed": False, "nodes": nodes, "edges": edges},
	}


def adjacency_from_edges(nodes: list[str],
			 edges: list[list[str]]) -> dict[str, set[str]]:
	adjacency = {node: set() for node in nodes}
	for edge in edges:
		if len(edge) != 2:
			continue
		left, right = str(edge[0]), str(edge[1])
		adjacency.setdefault(left, set()).add(right)
		adjacency.setdefault(right, set()).add(left)
	return adjacency


def connected_subset(nodes: list[str], edges: list[list[str]],
		     count: int) -> list[str] | None:
	adjacency = adjacency_from_edges(nodes, edges)
	for start in nodes:
		visited = []
		seen = {start}
		queue = deque([start])
		while queue and len(visited) < count:
			node = queue.popleft()
			visited.append(node)
			for neighbor in sorted(adjacency.get(node, ())):
				if neighbor not in seen:
					seen.add(neighbor)
					queue.append(neighbor)
		if len(visited) >= count:
			return visited[:count]
	return None


def is_connected(nodes: list[str], edges: list[list[str]]) -> bool:
	if not nodes:
		return False
	subset = connected_subset(nodes, edges, len(nodes))
	return subset is not None and set(subset) == set(nodes)


def check_definitions(distance: int) -> list[dict[str, Any]]:
	"""Return a compact rotated-code-style stabilizer set.

	Distance 3 uses eight checks, matching the 17-qubit rotated-surface-code
	footprint. Larger odd distances use a bounded nearest-neighbor pattern that
	preserves the d^2 data and d^2-1 check count requirement for experiment
	generation, but should be treated as a generated layout candidate rather
	than a site-validated device patch.
	"""
	if distance == 3:
		return [
			{"name": "X0", "type": "x", "data": [0, 1, 3, 4]},
			{"name": "X1", "type": "x", "data": [1, 2, 4, 5]},
			{"name": "X2", "type": "x", "data": [3, 4, 6, 7]},
			{"name": "X3", "type": "x", "data": [4, 5, 7, 8]},
			{"name": "Z0", "type": "z", "data": [0, 3]},
			{"name": "Z1", "type": "z", "data": [1, 2, 4, 5]},
			{"name": "Z2", "type": "z", "data": [3, 4, 6, 7]},
			{"name": "Z3", "type": "z", "data": [5, 8]},
		]

	checks = []
	max_checks = distance * distance - 1
	for row in range(distance):
		for col in range(distance - 1):
			if len(checks) >= max_checks:
				break
			left = row * distance + col
			right = left + 1
			checks.append({
				"name": f"Z{len([c for c in checks if c['type'] == 'z'])}",
				"type": "z",
				"data": [left, right],
			})
		if len(checks) >= max_checks:
			break
	for row in range(distance - 1):
		for col in range(distance):
			if len(checks) >= max_checks:
				break
			top = row * distance + col
			bottom = top + distance
			checks.append({
				"name": f"X{len([c for c in checks if c['type'] == 'x'])}",
				"type": "x",
				"data": [top, bottom],
			})
		if len(checks) >= max_checks:
			break
	return checks


def build_patch(distance: int, selected_qubits: list[str],
		coupling_graph: dict[str, Any]) -> dict[str, Any]:
	counts = required_qubits(distance)
	data_count = counts["data_qubits"]
	check_count = counts["check_qubits"]
	data_qubits = selected_qubits[:data_count]
	check_qubits = selected_qubits[data_count:data_count + check_count]
	checks = check_definitions(distance)

	edges = qhw_coupling_edges(coupling_graph)
	native_edges = {
		frozenset((str(edge[0]), str(edge[1])))
		for edge in edges
		if len(edge) == 2
	}
	missing_edges = []
	for check_index, check in enumerate(checks):
		check_physical = check_qubits[check_index]
		for data_index in check["data"]:
			data_physical = data_qubits[data_index]
			if frozenset((check_physical, data_physical)) not in native_edges:
				missing_edges.append([check_physical, data_physical])

	return {
		"schema": "qhw-qec-patch-v1",
		"code": "rotated_surface_code",
		"distance": distance,
		"counts": counts,
		"selected_qubits": selected_qubits,
		"data_qubits": {
			f"D{index}": qubit for index, qubit in enumerate(data_qubits)
		},
		"check_qubits": {
			checks[index]["name"]: qubit
			for index, qubit in enumerate(check_qubits)
		},
		"checks": checks,
		"native_patch": not missing_edges,
		"missing_native_edges": missing_edges,
	}


def select_patch_qubits(distance: int, patch_arg: list[str] | None,
			device_info: dict[str, Any],
			coupling_graph: dict[str, Any],
			dry_run: bool) -> list[str]:
	total = required_qubits(distance)["total_qubits"]
	if patch_arg is not None:
		if len(patch_arg) < total:
			raise ValueError(
				f"patch provides {len(patch_arg)} qubits, but distance "
				f"{distance} requires {total}")
		return patch_arg[:total]

	nodes = qhw_coupling_nodes(coupling_graph)
	edges = qhw_coupling_edges(coupling_graph)
	if not nodes:
		nodes = qhw_device_qubits(device_info)
	if not nodes and dry_run:
		nodes = [f"QB{index}" for index in range(1, 21)]
	if len(nodes) < total:
		raise ValueError(
			f"backend has {len(nodes)} candidate qubits, but distance "
			f"{distance} requires {total}")

	if edges:
		subset = connected_subset(nodes, edges, total)
		if subset:
			return subset
	if len(nodes) >= total:
		return nodes[:total]
	raise ValueError("failed to select a QEC patch")


def backend_operations(ctx: WorkflowContext) -> set[str]:
	if ctx.args.dry_run:
		return {"measure", "reset", "cx", "cz", "h", "x"}
	try:
		backend = ctx.backend.qiskit_backend(ctx.args.calibration_set_id)
		target = getattr(backend, "target", None)
		names = getattr(target, "operation_names", None)
		if names:
			return {str(name).lower() for name in names}
	except Exception:
		return set()
	return set()


def validate_capabilities(ctx: WorkflowContext, patch: dict[str, Any]) -> list[str]:
	warnings = []
	ops = backend_operations(ctx)
	if not ctx.args.dry_run and ctx.args.reset_mode == "hardware" and "reset" not in ops:
		raise RuntimeError(
			"backend target does not advertise reset; repeated-round QEC "
			"requires reset or --reset-mode none with --rounds 1")
	if not ctx.args.dry_run and "measure" not in ops:
		warnings.append("backend target did not advertise measure")
	if ctx.args.reset_mode == "none" and max(ctx.args.rounds) > 1:
		raise RuntimeError(
			"--reset-mode none only supports one round in this workflow")
	if ctx.args.require_native_patch and not patch["native_patch"]:
		raise RuntimeError(
			"selected patch is connected but does not provide every "
			"check-data interaction as a native coupling edge")
	if not patch["native_patch"]:
		warnings.append(
			"selected patch is not a native QEC patch; transpilation may route "
			"some check-data interactions")
	if ctx.args.decoder == "pymatching":
		warnings.append(
			"pymatching graph construction is not implemented yet; using "
			"simple logical parity decoding")
	return warnings


def add_check_round(circuit, *, checks: list[dict[str, Any]],
		    data_offset: int, check_offset: int, syndrome_offset: int,
		    round_index: int, checks_per_round: int,
		    reset_mode: str) -> None:
	for check_index, check in enumerate(checks):
		ancilla = check_offset + check_index
		cbit = syndrome_offset + round_index * checks_per_round + check_index
		if check["type"] == "x":
			circuit.h(ancilla)
			for data_index in check["data"]:
				circuit.cx(ancilla, data_offset + data_index)
			circuit.h(ancilla)
		else:
			for data_index in check["data"]:
				circuit.cx(data_offset + data_index, ancilla)
		circuit.measure(ancilla, cbit)
		if reset_mode == "hardware":
			circuit.reset(ancilla)


def build_memory_circuit(distance: int, rounds: int, basis: str,
			 patch: dict[str, Any], reset_mode: str,
			 idle_us: float | None, name: str):
	try:
		from qiskit import QuantumCircuit
	except Exception as exc:
		raise RuntimeError("qiskit is required for qec_memory.py") from exc

	counts = patch["counts"]
	data_count = counts["data_qubits"]
	check_count = counts["check_qubits"]
	total_qubits = counts["total_qubits"]
	total_clbits = rounds * check_count + data_count
	circuit = QuantumCircuit(total_qubits, total_clbits, name=name)

	if basis == "x":
		for qubit in range(data_count):
			circuit.h(qubit)

	for round_index in range(rounds):
		add_check_round(
			circuit,
			checks=patch["checks"],
			data_offset=0,
			check_offset=data_count,
			syndrome_offset=0,
			round_index=round_index,
			checks_per_round=check_count,
			reset_mode=reset_mode)
		if idle_us:
			for qubit in range(data_count):
				circuit.delay(idle_us, qubit, unit="us")

	if basis == "x":
		for qubit in range(data_count):
			circuit.h(qubit)

	data_cbit_offset = rounds * check_count
	for data_index in range(data_count):
		circuit.measure(data_index, data_cbit_offset + data_index)
	return circuit


def qec_dry_run_counts(width: int, shots: int, logical_error: float) -> dict[str, int]:
	good = int(round(shots * (1.0 - logical_error)))
	bad = shots - good
	counts = {"0" * width: good}
	if bad:
		counts[("1" * width)] = bad
	return {key: value for key, value in counts.items() if value}


def classical_bit(key: Any, width: int, cbit_index: int) -> int:
	bits = normalize_count_key(key, width)
	return int(bits[width - 1 - cbit_index])


def decode_counts(counts: dict[str, Any], *, rounds: int,
		  data_count: int, check_count: int) -> dict[str, Any]:
	total_width = rounds * check_count + data_count
	total_shots = sum(int(value) for value in counts.values())
	if total_shots == 0:
		return {
			"shots": 0,
			"logical_failures": 0,
			"logical_failure_rate": None,
			"mean_detection_event_rate": None,
			"check_detection_event_rates": {},
		}

	logical_failures = 0
	check_events = [0 for _ in range(check_count)]
	event_denominator = 0
	for raw_key, raw_value in counts.items():
		shots = int(raw_value)
		data_bits = [
			classical_bit(raw_key, total_width, rounds * check_count + index)
			for index in range(data_count)
		]
		if sum(data_bits) > data_count / 2:
			logical_failures += shots

		prev = [0 for _ in range(check_count)]
		for round_index in range(rounds):
			current = [
				classical_bit(raw_key, total_width,
					      round_index * check_count + check_index)
				for check_index in range(check_count)
			]
			for check_index, value in enumerate(current):
				if value ^ prev[check_index]:
					check_events[check_index] += shots
			prev = current
			event_denominator += shots

	check_rates = {
		f"C{index}": (
			check_events[index] / event_denominator
			if event_denominator else None)
		for index in range(check_count)
	}
	mean_rate = (
		sum(check_events) / (event_denominator * check_count)
		if event_denominator and check_count else None)
	return {
		"shots": total_shots,
		"logical_failures": logical_failures,
		"logical_failure_rate": logical_failures / total_shots,
		"mean_detection_event_rate": mean_rate,
		"check_detection_event_rates": check_rates,
	}


def run_memory_record(ctx: WorkflowContext, *, distance: int, rounds: int,
		      basis: str, patch: dict[str, Any], shots: int,
		      repetition: int) -> dict[str, Any]:
	cid = f"qec_memory_{basis}_d{distance}_r{rounds}_s{shots}_rep{repetition}"
	circuit = build_memory_circuit(
		distance, rounds, basis, patch, ctx.args.reset_mode,
		ctx.args.idle_us, cid)
	mapping = {
		index: qubit for index, qubit in enumerate(patch["selected_qubits"])
	}
	start = time.monotonic()
	try:
		if ctx.args.dry_run:
			qasm_files = ctx.write_qasm_artifacts(circuit, cid)
			width = rounds * patch["counts"]["check_qubits"] + patch["counts"]["data_qubits"]
			logical_error = min(
				0.45,
				ctx.args.dry_run_logical_error
				* max(1, rounds)
				* max(1, distance - 1))
			result = dry_run_result(
				cid,
				shots,
				qec_dry_run_counts(width, shots, logical_error),
				execution_seconds=shots * (0.001 + rounds * 0.0002))
			run = ctx.write_backend_result(cid, result, qasm_files)
		else:
			run = ctx.run_circuit(
				circuit,
				name=cid,
				qasm_name=cid,
				shots=shots,
				qubit_mapping=mapping)
		script_wall = time.monotonic() - start
		decoded = decode_counts(
			run.counts or {},
			rounds=rounds,
			data_count=patch["counts"]["data_qubits"],
			check_count=patch["counts"]["check_qubits"])
		return {
			"ok": run.ok,
			"basis": basis,
			"distance": distance,
			"rounds": rounds,
			"shots": shots,
			"repetition": repetition,
			"job_id": run.job_id,
			"decoder": (
				"simple"
				if ctx.args.decoder in {"simple", "pymatching"}
				else "none"),
			"counts": run.counts or {},
			"decoded": decoded if ctx.args.decoder != "none" else {},
			"metrics": {
				"script_wall_seconds": script_wall,
				"logical_failure_rate": (
					decoded.get("logical_failure_rate")
					if ctx.args.decoder != "none" else None),
				"mean_detection_event_rate": (
					decoded.get("mean_detection_event_rate")
					if ctx.args.decoder != "none" else None),
			},
			"files": run.files,
		}
	except Exception as exc:
		return {
			"ok": False,
			"basis": basis,
			"distance": distance,
			"rounds": rounds,
			"shots": shots,
			"repetition": repetition,
			"error": str(exc),
			"counts": {},
			"decoded": {},
			"metrics": {},
			"files": {},
		}


def build_analysis(records: list[dict[str, Any]],
		   config: dict[str, Any]) -> dict[str, Any]:
	by_case = {}
	for record in records:
		if not record.get("ok"):
			continue
		key = f"{record['basis']}:d{record['distance']}:r{record['rounds']}"
		item = by_case.setdefault(key, {
			"basis": record["basis"],
			"distance": record["distance"],
			"rounds": record["rounds"],
			"logical_failure_rates": [],
			"detection_event_rates": [],
		})
		lf = record.get("metrics", {}).get("logical_failure_rate")
		de = record.get("metrics", {}).get("mean_detection_event_rate")
		if lf is not None:
			item["logical_failure_rates"].append(float(lf))
		if de is not None:
			item["detection_event_rates"].append(float(de))

	for item in by_case.values():
		lfs = item.pop("logical_failure_rates")
		des = item.pop("detection_event_rates")
		item["mean_logical_failure_rate"] = (
			sum(lfs) / len(lfs) if lfs else None)
		item["mean_detection_event_rate"] = (
			sum(des) / len(des) if des else None)
		item["records"] = len(lfs)

	return {
		"schema": "qhw-qec-memory-analysis-v1",
		"intent": (
			"Run a repeated-round surface-code memory-style circuit and "
			"decode syndrome samples offline."),
		"config": config,
		"record_count": len(records),
		"successful_record_count": sum(
			1 for record in records if record.get("ok")),
		"failed_record_count": sum(
			1 for record in records if not record.get("ok")),
		"cases": by_case,
		"caveats": [
			"The first implementation uses a simple offline logical-parity "
			"decoder. The pymatching option is accepted for workflow "
			"compatibility but currently falls back to the simple decoder.",
			"Patch auto-selection checks connectedness. Use "
			"--require-native-patch to require every stabilizer interaction "
			"to be a native coupling edge.",
		],
	}


def render_analysis_markdown(analysis: dict[str, Any]) -> str:
	lines = [
		"# QEC Memory Analysis",
		"",
		analysis["intent"],
		"",
		"| Case | Mean logical failure rate | Mean detection event rate | Records |",
		"| --- | ---: | ---: | ---: |",
	]
	for key, item in analysis["cases"].items():
		lines.append(
			f"| `{key}` | {item['mean_logical_failure_rate']} | "
			f"{item['mean_detection_event_rate']} | {item['records']} |")
	lines += ["", "## Caveats", ""]
	for caveat in analysis["caveats"]:
		lines.append(f"- {caveat}")
	return "\n".join(lines) + "\n"


def add_script_args(parser: argparse.ArgumentParser) -> None:
	parser.add_argument("--distance", type=int, default=3)
	parser.add_argument("--rounds", type=parse_int_list,
			    default=parse_int_list("3"))
	parser.add_argument("--basis", type=parse_basis, default=parse_basis("both"))
	parser.add_argument("--patch", type=parse_patch, default=None)
	parser.add_argument("--shots", type=int, default=1000)
	parser.add_argument("--repetitions", type=int, default=1)
	parser.add_argument("--decoder", choices=DECODER_CHOICES, default="simple")
	parser.add_argument("--reset-mode", choices=RESET_MODE_CHOICES,
			    default="hardware")
	parser.add_argument("--idle-us", type=float, default=None)
	parser.add_argument("--require-native-patch", action="store_true")
	parser.add_argument("--dry-run-logical-error", type=float, default=0.02)


def main() -> int:
	ctx = WorkflowContext.from_cli(
		__file__,
		description="Run a surface-code memory-style QEC workflow.",
		add_args=add_script_args,
		calibration=True,
		execution=True,
		dry_run=True,
	)
	args = ctx.args
	if args.distance < 3 or args.distance % 2 == 0:
		raise ValueError("--distance must be an odd integer >= 3")
	if args.shots < 1:
		raise ValueError("--shots must be at least 1")
	if args.repetitions < 1:
		raise ValueError("--repetitions must be at least 1")

	backend_info = {} if args.dry_run else to_jsonable(
		ctx.backend.get_backend_info())
	device_info = {} if args.dry_run else to_jsonable(
		ctx.backend.get_device_info())
	coupling_graph = (
		synthetic_coupling_graph()
		if args.dry_run
		else to_jsonable(ctx.backend.get_coupling_graph(args.calibration_set_id)))
	selected_qubits = select_patch_qubits(
		args.distance, args.patch, device_info, coupling_graph, args.dry_run)
	if not is_connected(selected_qubits, qhw_coupling_edges(coupling_graph)):
		raise ValueError("selected QEC patch is not connected")
	patch = build_patch(args.distance, selected_qubits, coupling_graph)
	warnings = validate_capabilities(ctx, patch)

	backend_info_file = ctx.paths.root / "backend_info.json"
	device_info_file = qhw_json_path(ctx.paths.root, "device_info")
	coupling_file = qhw_json_path(ctx.paths.root, "coupling_graph")
	patch_file = ctx.paths.root / "patch.json"
	records_file = ctx.paths.results / "syndrome_records.jsonl"
	decoder_file = ctx.paths.results / "decoder_records.jsonl"
	analysis_file = ctx.paths.results / "analysis.json"
	analysis_md_file = ctx.paths.results / "analysis.md"
	summary_file = ctx.paths.results / "qec_memory_summary.json"
	ctx.write_json(backend_info_file, backend_info)
	ctx.write_json(device_info_file, device_info)
	ctx.write_json(coupling_file, coupling_graph)
	ctx.write_json(patch_file, patch)

	records = []
	for repetition in range(args.repetitions):
		for basis in args.basis:
			for rounds in args.rounds:
				records.append(run_memory_record(
					ctx,
					distance=args.distance,
					rounds=int(rounds),
					basis=basis,
					patch=patch,
					shots=args.shots,
					repetition=repetition))

	write_jsonl(records_file, records)
	write_jsonl(decoder_file, [
		{
			"basis": record["basis"],
			"distance": record["distance"],
			"rounds": record["rounds"],
			"repetition": record["repetition"],
			"decoder": record.get("decoder"),
			"decoded": record.get("decoded", {}),
		}
		for record in records
	])
	config = {
		"backend": ctx.backend_name,
		"distance": args.distance,
		"rounds": args.rounds,
		"basis": args.basis,
		"shots": args.shots,
		"repetitions": args.repetitions,
		"decoder": args.decoder,
		"reset_mode": args.reset_mode,
		"idle_us": args.idle_us,
		"require_native_patch": args.require_native_patch,
		"dry_run": args.dry_run,
		"warnings": warnings,
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
		"distance": args.distance,
		"rounds": args.rounds,
		"basis": args.basis,
		"native_patch": patch["native_patch"],
		"warnings": warnings,
		"files": {
			"backend_info": str(backend_info_file),
			"device_info": str(device_info_file),
			"coupling_graph": str(coupling_file),
			"patch": str(patch_file),
			"syndrome_records": str(records_file),
			"decoder_records": str(decoder_file),
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
			f"native patch: {summary['native_patch']}",
			f"output: {ctx.paths.root}",
		],
	)


if __name__ == "__main__":
	raise SystemExit(main())
