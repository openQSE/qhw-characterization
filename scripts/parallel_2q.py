#!/usr/bin/env python3
"""Measure whether disjoint two-qubit gates execute in parallel."""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from qhw_util.output import backend_result_qhw
from qhw_util.output import qhw_json_path
from qhw_util.output import to_jsonable
from qhw_util.schema import qhw_coupling_edges
from qhw_util.schema import qhw_coupling_nodes
from qhw_util.timing_model import execution_per_shot
from qhw_util.timing_model import expected_model_summary
from qhw_util.timing_model import one_q_baseline_table
from qhw_util.timing_model import parallel_two_q_sequence_model
from qhw_util.timing_model import two_q_baseline_key
from qhw_util.timing_model import two_q_baseline_table
from qhw_util.workflow import WorkflowContext

SUPPORTED_QISKIT_2Q_GATES = ("cz", "cx", "cnot", "swap", "ecr", "rxx", "ryy", "rzz")
PRIMARY_METRIC = "execution_per_shot_seconds"
DIAGNOSTIC_METRICS = (
	"script_wall_seconds",
	"client_total_seconds",
	"server_total_seconds",
)


def parse_int_list(value: str) -> list[int]:
	items = []
	for raw in value.split(","):
		raw = raw.strip()
		if not raw:
			continue
		item = int(raw)
		if item < 1:
			raise argparse.ArgumentTypeError(
				f"list values must be positive integers: {value!r}")
		items.append(item)
	if not items:
		raise argparse.ArgumentTypeError("list must contain at least one value")
	return items


def parse_gate_list(value: str) -> list[str]:
	gates = []
	for raw in value.split(","):
		gate = raw.strip().lower()
		if not gate:
			continue
		if gate != "auto" and gate not in SUPPORTED_QISKIT_2Q_GATES:
			raise argparse.ArgumentTypeError(
				f"unsupported 2Q gate {gate!r}; expected auto or one of "
				f"{', '.join(SUPPORTED_QISKIT_2Q_GATES)}")
		gates.append(gate)
	if not gates:
		raise argparse.ArgumentTypeError("at least one gate is required")
	return gates


def parse_angle(value: str) -> float:
	if value.lower() == "pi":
		return math.pi
	if value.lower() in ("pi/2", "half-pi"):
		return math.pi / 2
	return float(value)


def synthetic_coupling_graph() -> dict[str, Any]:
	nodes = [f"QB{index}" for index in range(1, 21)]
	edges = [
		["QB1", "QB2"], ["QB1", "QB4"], ["QB2", "QB5"],
		["QB3", "QB4"], ["QB3", "QB8"], ["QB4", "QB5"],
		["QB4", "QB9"], ["QB5", "QB6"], ["QB5", "QB10"],
		["QB6", "QB7"], ["QB6", "QB11"], ["QB7", "QB12"],
		["QB8", "QB9"], ["QB8", "QB13"], ["QB9", "QB10"],
		["QB9", "QB14"], ["QB10", "QB11"], ["QB10", "QB15"],
		["QB11", "QB12"], ["QB11", "QB16"], ["QB12", "QB17"],
		["QB13", "QB14"], ["QB14", "QB15"], ["QB15", "QB16"],
		["QB16", "QB17"], ["QB17", "QB18"], ["QB18", "QB19"],
		["QB19", "QB20"],
	]
	return {
		"schema": "qhw-coupling-v1",
		"provider": "dry-run",
		"device": {"id": "dry-run", "provider": "dry-run"},
		"coupling": {"directed": False, "nodes": nodes, "edges": edges},
		"operations": [{"name": "cz", "loci": edges}],
		"extensions": {},
	}


def operation_loci_by_gate(coupling_graph: dict[str, Any]) -> dict[str, list[list[str]]]:
	result: dict[str, list[list[str]]] = {}
	for operation in coupling_graph.get("operations") or []:
		name = str(operation.get("name", "")).lower()
		loci = []
		for locus in operation.get("loci") or []:
			if isinstance(locus, (list, tuple)) and len(locus) == 2:
				loci.append([str(locus[0]), str(locus[1])])
		if name and loci:
			result[name] = loci
	return result


def resolve_gates(value: list[str], coupling_graph: dict[str, Any]) -> list[str]:
	if value != ["auto"]:
		return value
	ops = operation_loci_by_gate(coupling_graph)
	for preferred in ("cz", "cx", "cnot", "ecr", "rzz", "rxx", "ryy"):
		if preferred in ops:
			return [preferred]
	return ["cz"]


def edges_for_gate(gate: str, coupling_graph: dict[str, Any]) -> list[list[str]]:
	op_edges = operation_loci_by_gate(coupling_graph).get(gate, [])
	edges = op_edges or qhw_coupling_edges(coupling_graph)
	seen = set()
	result = []
	for edge in edges:
		if len(edge) != 2:
			continue
		key = tuple(sorted((str(edge[0]), str(edge[1]))))
		if key in seen:
			continue
		seen.add(key)
		result.append([str(edge[0]), str(edge[1])])
	return result


def greedy_matching(edges: list[list[str]], start: int = 0) -> list[list[str]]:
	if not edges:
		return []
	ordered = edges[start:] + edges[:start]
	used = set()
	matching = []
	for left, right in ordered:
		if left in used or right in used:
			continue
		matching.append([left, right])
		used.add(left)
		used.add(right)
	return matching


def resolve_matching_sizes(value: str, max_size: int) -> list[int]:
	sizes = []
	for raw in value.split(","):
		raw = raw.strip().lower()
		if not raw:
			continue
		if raw == "max":
			sizes.append(max_size)
		elif raw == "all":
			sizes.extend(range(1, max_size + 1))
		else:
			size = int(raw)
			if size < 1:
				raise ValueError(f"matching size must be positive: {raw!r}")
			if size > max_size:
				raise ValueError(
					f"matching size {size} exceeds max matching size {max_size}")
			sizes.append(size)
	if not sizes:
		raise ValueError("at least one matching size is required")
	return sorted(set(sizes))


def select_matchings(edges: list[list[str]], sizes: list[int],
		     per_size: int, seed: int) -> dict[int, list[list[list[str]]]]:
	if per_size < 1:
		raise ValueError("--matchings-per-size must be at least 1")
	rng = random.Random(seed)
	result: dict[int, list[list[list[str]]]] = {size: [] for size in sizes}
	seen: dict[int, set[tuple[tuple[str, str], ...]]] = {
		size: set() for size in sizes
	}
	offsets = list(range(len(edges)))
	rng.shuffle(offsets)
	for start in offsets:
		matching = greedy_matching(edges, start)
		for size in sizes:
			if len(result[size]) >= per_size:
				continue
			if len(matching) < size:
				continue
			selected = matching[:size]
			key = tuple(sorted(tuple(edge) for edge in selected))
			if key in seen[size]:
				continue
			seen[size].add(key)
			result[size].append(selected)
	if any(not value for value in result.values()):
		missing = [size for size, value in result.items() if not value]
		raise ValueError(f"failed to select matchings for sizes: {missing}")
	return result


def apply_2q_gate(circuit, gate: str, left: int, right: int, angle: float) -> None:
	if gate == "cz":
		circuit.cz(left, right)
	elif gate in ("cx", "cnot"):
		circuit.cx(left, right)
	elif gate == "swap":
		circuit.swap(left, right)
	elif gate == "ecr":
		circuit.ecr(left, right)
	elif gate == "rxx":
		circuit.rxx(angle, left, right)
	elif gate == "ryy":
		circuit.ryy(angle, left, right)
	elif gate == "rzz":
		circuit.rzz(angle, left, right)
	else:
		raise ValueError(f"unsupported 2Q gate: {gate}")


def apply_1q_gate(circuit, gate: str, qubit: int, angle: float) -> None:
	if gate == "x":
		circuit.x(qubit)
	elif gate == "rx":
		circuit.rx(angle, qubit)
	elif gate == "ry":
		circuit.ry(angle, qubit)
	else:
		raise ValueError(f"unsupported 1Q gate: {gate}")


def build_single_1q_circuit(gate: str, angle: float, name: str):
	try:
		from qiskit import QuantumCircuit
	except Exception as exc:
		raise RuntimeError(
			"qiskit is required for parallel_2q.py") from exc
	circuit = QuantumCircuit(1, 1, name=name)
	apply_1q_gate(circuit, gate, 0, angle)
	circuit.measure(0, 0)
	return circuit


def build_single_2q_circuit(gate: str, angle: float, name: str):
	try:
		from qiskit import QuantumCircuit
	except Exception as exc:
		raise RuntimeError(
			"qiskit is required for parallel_2q.py") from exc
	circuit = QuantumCircuit(2, 2, name=name)
	apply_2q_gate(circuit, gate, 0, 1, angle)
	circuit.measure(range(2), range(2))
	return circuit


def build_parallel_2q_circuit(gate: str, matching: list[list[str]],
			      depth: int, angle: float, name: str):
	try:
		from qiskit import QuantumCircuit
	except Exception as exc:
		raise RuntimeError(
			"qiskit is required for parallel_2q.py") from exc
	width = len(matching) * 2
	circuit = QuantumCircuit(width, width, name=name)
	for _ in range(depth):
		for qubit in range(width):
			apply_1q_gate(circuit, "rx", qubit, angle)
		for index in range(len(matching)):
			apply_2q_gate(circuit, gate, 2 * index, 2 * index + 1, angle)
		for qubit in range(width):
			apply_1q_gate(circuit, "ry", qubit, angle)
		if width > 2:
			circuit.barrier()
	circuit.measure(range(width), range(width))
	return circuit


def matching_mapping(matching: list[list[str]]) -> dict[int, str]:
	mapping = {}
	for index, (left, right) in enumerate(matching):
		mapping[2 * index] = left
		mapping[2 * index + 1] = right
	return mapping


def matching_key(matching: list[list[str]]) -> str:
	return ",".join(f"{left}-{right}" for left, right in matching)


def dry_run_1q_execution_seconds(gate: str, shots: int) -> float:
	per_gate = {
		"x": 0.00055,
		"rx": 0.00070,
		"ry": 0.00075,
	}
	return shots * per_gate[gate]


def dry_run_2q_execution_seconds(gate: str, shots: int) -> float:
	per_gate = {
		"cz": 0.0030,
		"cx": 0.0034,
		"cnot": 0.0034,
		"swap": 0.0045,
		"ecr": 0.0036,
		"rxx": 0.0037,
		"ryy": 0.0037,
		"rzz": 0.0035,
	}
	return shots * per_gate.get(gate, 0.0035)


def dry_run_result(cid: str, shots: int, depth: int,
		   matching_size: int, gate: str,
		   execution_seconds: float | None = None) -> dict[str, Any]:
	if execution_seconds is None:
		parallel_layer = (
			dry_run_1q_execution_seconds("rx", 1)
			+ dry_run_2q_execution_seconds(gate, 1)
			+ dry_run_1q_execution_seconds("ry", 1)
		)
		congestion_seconds = 0.00015 * max(0, matching_size - 1)
		execution_seconds = shots * depth * (
			parallel_layer + congestion_seconds)
	return {
		"cid": cid,
		"result": {
			"qhw_result": {
				"schema": "qhw-result-v1",
				"provider": "dry-run",
				"device": {"id": "dry-run", "provider": "dry-run"},
				"job": {"id": cid, "status": "completed"},
				"result": {
					"shots": shots,
					"counts": {},
					"success": True,
				},
				"timing": {
					"timestamps": {},
					"timeline": [],
					"durations_seconds": {
						"execution_seconds": execution_seconds,
						"provider_total_seconds": execution_seconds,
					},
				},
				"errors": [],
				"extensions": {},
				"raw": {"included": False, "format": None, "artifacts": []},
			},
		},
		"rc": 0,
	}


def safe_float(value: Any) -> float | None:
	if value is None:
		return None
	try:
		return float(value)
	except (TypeError, ValueError):
		return None


def extract_metrics(script_wall_seconds: float,
		    result: dict[str, Any],
		    shots: int) -> dict[str, float | None]:
	qhw_result = backend_result_qhw(result)
	if not qhw_result:
		raise ValueError("backend result did not include normalized qhw_result")
	timing = qhw_result.get("timing", {})
	durations = timing.get("durations_seconds", {})
	execution_seconds = safe_float(durations.get("execution_seconds"))
	return {
		"script_wall_seconds": script_wall_seconds,
		"client_total_seconds": None,
		"server_total_seconds": durations.get("provider_total_seconds"),
		"execution_seconds": execution_seconds,
		PRIMARY_METRIC: (
			execution_seconds / shots
			if execution_seconds is not None and shots else None),
	}


def metric_points(records: list[dict[str, Any]],
		  x_field: str,
		  metric: str) -> list[tuple[float, float]]:
	points = []
	for record in records:
		value = safe_float(record.get("metrics", {}).get(metric))
		x_value = safe_float(record.get(x_field))
		if value is not None and x_value is not None:
			points.append((x_value, value))
	return sorted(points)


def linear_fit(points: list[tuple[float, float]]) -> dict[str, Any] | None:
	if len(points) < 2:
		return None
	n = len(points)
	xs = [point[0] for point in points]
	ys = [point[1] for point in points]
	sx = sum(xs)
	sy = sum(ys)
	sxx = sum(x * x for x in xs)
	sxy = sum(x * y for x, y in points)
	denom = n * sxx - sx * sx
	if denom == 0:
		return None
	slope = (n * sxy - sx * sy) / denom
	intercept = (sy - slope * sx) / n
	residuals = [y - (intercept + slope * x) for x, y in points]
	rms = (sum(value * value for value in residuals) / n) ** 0.5
	return {
		"intercept_seconds": intercept,
		"slope_seconds_per_unit": slope,
		"rms_residual_seconds": rms,
		"points": n,
		"x_min": min(xs),
		"x_max": max(xs),
		"y_mean": statistics.fmean(ys),
	}


def group_records(records: list[dict[str, Any]],
		  field: str) -> dict[str, list[dict[str, Any]]]:
	groups: dict[str, list[dict[str, Any]]] = {}
	for record in records:
		groups.setdefault(str(record[field]), []).append(record)
	return groups


def classify_matching_fit(fit: dict[str, Any] | None) -> dict[str, Any]:
	if not fit:
		return {
			"status": "insufficient_data",
			"conclusion": "Not enough matching sizes to assess 2Q parallelism.",
		}
	span = fit["x_max"] - fit["x_min"]
	predicted_delta = fit["slope_seconds_per_unit"] * span
	mean_value = abs(fit["y_mean"]) if fit["y_mean"] else 0.0
	delta_fraction = abs(predicted_delta) / mean_value if mean_value else None
	if delta_fraction is not None and delta_fraction < 0.10:
		status = "matching_size_independent"
		conclusion = (
			"Execution time per shot is approximately independent of the "
			"number of simultaneous disjoint 2Q gates.")
	elif fit["slope_seconds_per_unit"] > 0:
		status = "matching_size_dependent"
		conclusion = (
			"Execution time per shot increases with simultaneous disjoint "
			"2Q gate count.")
	else:
		status = "not_matching_size_increasing"
		conclusion = (
			"Execution time per shot did not increase with matching size.")
	return {
		"status": status,
		"conclusion": conclusion,
		"predicted_delta_seconds": predicted_delta,
		"delta_fraction_of_mean": delta_fraction,
	}


def build_analysis(records: list[dict[str, Any]],
		   config: dict[str, Any],
		   plots: dict[str, Any]) -> dict[str, Any]:
	successful = [
		record for record in records
		if record.get("ok")
		and safe_float(record.get("metrics", {}).get(PRIMARY_METRIC)) is not None
	]
	by_gate_depth = {}
	for gate, gate_records in group_records(successful, "gate").items():
		for depth, group in group_records(gate_records, "depth").items():
			key = f"{gate}:depth{depth}"
			fit = linear_fit(
				metric_points(group, "matching_size", PRIMARY_METRIC))
			by_gate_depth[key] = {
				"record_count": len(group),
				"matching_sizes": sorted(
					{record["matching_size"] for record in group}),
				"fit": fit,
				"classification": classify_matching_fit(fit),
			}
	status_counts: dict[str, int] = {}
	for item in by_gate_depth.values():
		status = item["classification"]["status"]
		status_counts[status] = status_counts.get(status, 0) + 1
	if not successful:
		status = "no_hardware_execution_timing"
		conclusion = "No successful record contained hardware execution timing."
	elif status_counts.get("matching_size_dependent", 0):
		status = "parallel_congestion_observed"
		conclusion = (
			"At least one 2Q layer group shows timing growth with "
			"simultaneous disjoint edge count.")
	else:
		status = "parallel_congestion_not_established"
		conclusion = (
			"This run did not establish a strong matching-size timing term.")
	return {
		"schema": "qhw-parallel-2q-analysis-v1",
		"intent": (
			"Determine whether disjoint two-qubit gate layers execute in "
			"parallel by sweeping matching size at fixed depth while using "
			"1Q interleaves to reduce cancellation opportunities."),
		"primary_metric": PRIMARY_METRIC,
		"primary_metric_definition": (
			"Provider timeline execution duration divided by shots. The "
			"execution duration is the interval from execution_started to "
			"execution_ended in the normalized qhw result timeline."),
		"diagnostic_metrics_not_used_for_hardware_conclusion": list(
			DIAGNOSTIC_METRICS),
		"config": config,
		"record_count": len(records),
		"successful_record_count": len(successful),
		"failed_record_count": sum(
			1 for record in records if not record.get("ok")),
		"overall": {
			"status": status,
			"conclusion": conclusion,
			"gate_depth_status_counts": status_counts,
		},
		"by_gate_depth": by_gate_depth,
		"expected_model_summary": expected_model_summary(records),
		"plots": plots,
		"caveats": [
			"Compilation may transform or route two-qubit layers.",
			"The script inserts rx/ry 1Q layers around each 2Q layer to "
			"reduce repeated-gate cancellation opportunities.",
			"Timing parallelism does not imply fidelity is unchanged.",
			"Selected matchings are sampled, not an exhaustive layout search.",
		],
	}


def plot_records(records: list[dict[str, Any]],
		 plots_dir: Path) -> dict[str, Any]:
	valid = [
		record for record in records
		if record.get("ok")
		and safe_float(record.get("metrics", {}).get(PRIMARY_METRIC)) is not None
	]
	if not valid:
		return {
			"status": "skipped",
			"reason": "no successful records with hardware execution timing",
			"files": [],
		}
	plots_dir.mkdir(parents=True, exist_ok=True)
	mpl_config_dir = plots_dir / ".matplotlib"
	mpl_config_dir.mkdir(parents=True, exist_ok=True)
	os.environ.setdefault("MPLCONFIGDIR", str(mpl_config_dir))
	try:
		import matplotlib
		matplotlib.use("Agg")
		import matplotlib.pyplot as plt
	except Exception as exc:
		return {
			"status": "skipped",
			"reason": f"matplotlib unavailable: {exc}",
			"files": [],
		}

	files = []

	def metric(record):
		return safe_float(record["metrics"].get(PRIMARY_METRIC))

	def write_plot(name: str):
		path = plots_dir / name
		plt.tight_layout()
		plt.savefig(path, dpi=150)
		plt.close()
		files.append(str(path))

	for gate, gate_records in sorted(group_records(valid, "gate").items()):
		plt.figure(figsize=(9, 5))
		for size, group in sorted(
				group_records(gate_records, "matching_size").items(),
				key=lambda item: int(item[0])):
			aggregate: dict[int, list[float]] = {}
			for record in group:
				value = metric(record)
				if value is not None:
					aggregate.setdefault(record["depth"], []).append(value)
			points = sorted(
				(depth, statistics.fmean(values))
				for depth, values in aggregate.items())
			plt.plot(
				[point[0] for point in points],
				[point[1] for point in points],
				marker="o",
				label=f"matching size {size}")
		plt.xlabel("Repeated 2Q layer depth")
		plt.ylabel("Hardware execution time per shot (s)")
		plt.title(f"Parallel 2Q {gate} timing by matching size")
		plt.xscale("log", base=2)
		plt.grid(True, which="both", alpha=0.3)
		plt.legend(fontsize="small")
		write_plot(f"parallel_2q_{gate}_depth_by_matching_size.png")

	for gate, gate_records in sorted(group_records(valid, "gate").items()):
		plt.figure(figsize=(9, 5))
		for depth, group in sorted(
				group_records(gate_records, "depth").items(),
				key=lambda item: int(item[0])):
			aggregate: dict[int, list[float]] = {}
			for record in group:
				value = metric(record)
				if value is not None:
					aggregate.setdefault(
						record["matching_size"], []).append(value)
			points = sorted(
				(size, statistics.fmean(values))
				for size, values in aggregate.items())
			plt.plot(
				[point[0] for point in points],
				[point[1] for point in points],
				marker="o",
				label=f"depth {depth}")
		plt.xlabel("Simultaneous disjoint 2Q gates")
		plt.ylabel("Hardware execution time per shot (s)")
		plt.title(f"Parallel 2Q {gate} matching-size scaling")
		plt.grid(True, alpha=0.3)
		plt.legend(fontsize="small")
		write_plot(f"parallel_2q_{gate}_matching_size_scaling.png")

	return {
		"status": "generated",
		"metric": PRIMARY_METRIC,
		"files": files,
	}


def render_analysis_markdown(analysis: dict[str, Any]) -> str:
	lines = [
		"# Parallel 2Q Timing Analysis",
		"",
		"## Intent",
		"",
		analysis["intent"],
		"",
		"## Overall Result",
		"",
		f"Status: `{analysis['overall']['status']}`",
		"",
		analysis["overall"]["conclusion"],
		"",
		f"Records: {analysis['record_count']}",
		f"Successful records: {analysis['successful_record_count']}",
		f"Failed records: {analysis['failed_record_count']}",
		"",
		"## Gate/Depth Matching-Size Fits",
		"",
		"| Gate/Depth | Status | Matching sizes | Slope (s/edge/shot) | Conclusion |",
		"| --- | --- | --- | ---: | --- |",
	]
	for key, item in analysis["by_gate_depth"].items():
		fit = item.get("fit") or {}
		classification = item["classification"]
		lines.append(
			f"| `{key}` | `{classification['status']}` | "
			f"{item['matching_sizes']} | "
			f"{fit.get('slope_seconds_per_unit', '')} | "
			f"{classification['conclusion']} |")
	lines += [
		"",
		"## Baseline Model Residuals",
		"",
		"| Residual | Samples | Mean error (s/shot) | Mean absolute error (s/shot) | RMS error (s/shot) |",
		"| --- | ---: | ---: | ---: | ---: |",
	]
	model_summary = analysis.get("expected_model_summary", {})
	if model_summary:
		for key, item in model_summary.items():
			lines.append(
				f"| `{key}` | {item['count']} | "
				f"{item['mean_error_seconds']} | "
				f"{item['mean_absolute_error_seconds']} | "
				f"{item['rms_error_seconds']} |")
	else:
		lines.append("| none | 0 |  |  |  |")
	lines += ["", "## Plots", ""]
	plots = analysis.get("plots", {})
	if plots.get("status") == "generated":
		for path in plots.get("files", []):
			lines.append(f"- `{path}`")
	else:
		lines.append(
			f"Plot generation was skipped: {plots.get('reason', 'unknown')}")
	lines += ["", "## Caveats", ""]
	for caveat in analysis["caveats"]:
		lines.append(f"- {caveat}")
	return "\n".join(lines) + "\n"


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	text = "\n".join(
		json.dumps(to_jsonable(record), sort_keys=True)
		for record in records)
	if text:
		text += "\n"
	path.write_text(text)


def add_script_args(parser: argparse.ArgumentParser) -> None:
	parser.add_argument("--gates", type=parse_gate_list,
			    default=parse_gate_list("auto"))
	parser.add_argument("--matching-sizes", default="1,2,max")
	parser.add_argument("--matchings-per-size", type=int, default=2)
	parser.add_argument("--depths", type=parse_int_list,
			    default=parse_int_list("1,2,4,8,16,32,64,128"))
	parser.add_argument("--shots", type=int, default=100)
	parser.add_argument("--repetitions", type=int, default=1)
	parser.add_argument("--sample-seed", type=int, default=1)
	parser.add_argument("--angle", type=parse_angle, default=math.pi)


def main() -> int:
	ctx = WorkflowContext.from_cli(
		__file__,
		description=(
			"Measure simultaneous disjoint two-qubit gate layer timing."),
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
	if args.matchings_per_size < 1:
		raise ValueError("--matchings-per-size must be at least 1")

	backend_info = {} if args.dry_run else to_jsonable(
		ctx.backend.get_backend_info())
	coupling_graph = synthetic_coupling_graph() if args.dry_run else to_jsonable(
		ctx.backend.get_coupling_graph(args.calibration_set_id))
	gates = resolve_gates(args.gates, coupling_graph)

	coupling_graph_file = qhw_json_path(ctx.paths.root, "coupling_graph")
	selected_matchings_file = ctx.paths.root / "selected_matchings.json"
	backend_info_file = ctx.paths.root / "backend_info.json"
	baseline_1q_records_file = ctx.paths.results / "baseline_1q_records.jsonl"
	baseline_2q_records_file = ctx.paths.results / "baseline_2q_records.jsonl"
	records_file = ctx.paths.results / "timing_records.jsonl"
	summary_file = ctx.paths.results / "timing_summary.json"
	analysis_file = ctx.paths.results / "analysis.json"
	analysis_md_file = ctx.paths.results / "analysis.md"
	plots_dir = ctx.paths.results / "plots"
	ctx.write_json(backend_info_file, backend_info)
	ctx.write_json(coupling_graph_file, coupling_graph)

	selected_by_gate = {}
	for gate in gates:
		edges = edges_for_gate(gate, coupling_graph)
		max_matching = len(greedy_matching(edges))
		sizes = resolve_matching_sizes(args.matching_sizes, max_matching)
		selected_by_gate[gate] = {
			"edges": edges,
			"max_matching_size": max_matching,
			"sizes": sizes,
			"matchings": select_matchings(
				edges, sizes, args.matchings_per_size, args.sample_seed),
		}
	ctx.write_json(selected_matchings_file, selected_by_gate)

	involved_qubits = sorted({
		str(qubit)
		for selection in selected_by_gate.values()
		for matchings in selection["matchings"].values()
		for matching in matchings
		for edge in matching
		for qubit in edge
	})
	selected_edges_by_gate: dict[str, list[list[str]]] = {}
	for gate, selection in selected_by_gate.items():
		seen = set()
		selected_edges = []
		for matchings in selection["matchings"].values():
			for matching in matchings:
				for edge in matching:
					key = tuple(edge)
					if key in seen:
						continue
					seen.add(key)
					selected_edges.append(edge)
		selected_edges_by_gate[gate] = selected_edges

	baseline_1q_records = []
	for repetition in range(args.repetitions):
		for qubit in involved_qubits:
			for baseline_gate in ("rx", "ry"):
				cid = (
					f"baseline_parallel_2q_1q_{qubit}_{baseline_gate}_"
					f"s{args.shots}_r{repetition}")
				circuit = build_single_1q_circuit(
					baseline_gate, args.angle, cid)
				start = time.monotonic()
				try:
					if args.dry_run:
						qasm_files = ctx.write_qasm_artifacts(
							circuit, cid)
						result = dry_run_result(
							cid,
							args.shots,
							1,
							1,
							baseline_gate,
							dry_run_1q_execution_seconds(
								baseline_gate, args.shots))
						run = ctx.write_backend_result(
							cid, result, qasm_files)
					else:
						run = ctx.run_circuit(
							circuit,
							name=cid,
							qasm_name=cid,
							shots=args.shots,
							qubit_mapping={0: qubit})
					script_wall = time.monotonic() - start
					metrics = extract_metrics(
						script_wall, run.result, args.shots)
					record = {
						"ok": True,
						"circuit_id": cid,
						"experiment": "parallel_2q_single_1q_baseline",
						"submission_path": "backend.run",
						"gate": baseline_gate,
						"width": 1,
						"depth": 1,
						"shots": args.shots,
						"repetition": repetition,
						"physical_qubit": qubit,
						"physical_qubits": [qubit],
						"qubit_mapping": {"0": qubit},
						"job_id": run.job_id,
						"metrics": metrics,
						"counts": run.counts,
						"files": run.files,
					}
				except Exception as exc:
					record = {
						"ok": False,
						"circuit_id": cid,
						"experiment": "parallel_2q_single_1q_baseline",
						"gate": baseline_gate,
						"width": 1,
						"depth": 1,
						"shots": args.shots,
						"repetition": repetition,
						"physical_qubit": qubit,
						"physical_qubits": [qubit],
						"qubit_mapping": {"0": qubit},
						"error": str(exc),
						"metrics": {},
						"files": {},
					}
				baseline_1q_records.append(record)

	baseline_2q_records = []
	for repetition in range(args.repetitions):
		for gate, edges in selected_edges_by_gate.items():
			for edge in edges:
				cid = (
					f"baseline_parallel_2q_{gate}_{edge[0]}-{edge[1]}_"
					f"s{args.shots}_r{repetition}")
				circuit = build_single_2q_circuit(gate, args.angle, cid)
				mapping = {0: edge[0], 1: edge[1]}
				start = time.monotonic()
				try:
					if args.dry_run:
						qasm_files = ctx.write_qasm_artifacts(
							circuit, cid)
						result = dry_run_result(
							cid,
							args.shots,
							1,
							1,
							gate,
							dry_run_2q_execution_seconds(gate, args.shots))
						run = ctx.write_backend_result(
							cid, result, qasm_files)
					else:
						run = ctx.run_circuit(
							circuit,
							name=cid,
							qasm_name=cid,
							shots=args.shots,
							qubit_mapping=mapping)
					script_wall = time.monotonic() - start
					metrics = extract_metrics(
						script_wall, run.result, args.shots)
					record = {
						"ok": True,
						"circuit_id": cid,
						"experiment": "parallel_2q_single_2q_baseline",
						"baseline_key": two_q_baseline_key(gate, edge),
						"submission_path": "backend.run",
						"gate": gate,
						"depth": 1,
						"shots": args.shots,
						"repetition": repetition,
						"physical_pair": edge,
						"qubit_mapping": {
							str(key): value for key, value in mapping.items()
						},
						"job_id": run.job_id,
						"metrics": metrics,
						"counts": run.counts,
						"files": run.files,
					}
				except Exception as exc:
					record = {
						"ok": False,
						"circuit_id": cid,
						"experiment": "parallel_2q_single_2q_baseline",
						"baseline_key": two_q_baseline_key(gate, edge),
						"gate": gate,
						"depth": 1,
						"shots": args.shots,
						"repetition": repetition,
						"physical_pair": edge,
						"qubit_mapping": {
							str(key): value for key, value in mapping.items()
						},
						"error": str(exc),
						"metrics": {},
						"files": {},
					}
				baseline_2q_records.append(record)

	one_q_baselines = one_q_baseline_table(baseline_1q_records)
	two_q_baselines = two_q_baseline_table(baseline_2q_records)

	records = []
	for repetition in range(args.repetitions):
		for gate, selection in selected_by_gate.items():
			for matching_size, matchings in selection["matchings"].items():
				for matching_index, matching in enumerate(matchings):
					mapping = matching_mapping(matching)
					edge_key = matching_key(matching)
					for depth in args.depths:
						cid = (
							f"parallel_2q_{gate}_m{matching_size}_"
							f"i{matching_index}_d{depth}_s{args.shots}_"
							f"r{repetition}")
						circuit = build_parallel_2q_circuit(
							gate, matching, depth, args.angle, cid)
						start = time.monotonic()
						try:
							if args.dry_run:
								qasm_files = ctx.write_qasm_artifacts(
									circuit, cid)
								result = dry_run_result(
									cid, args.shots, depth, matching_size, gate)
								run = ctx.write_backend_result(
									cid, result, qasm_files)
							else:
								run = ctx.run_circuit(
									circuit,
									name=cid,
									qasm_name=cid,
									shots=args.shots,
									qubit_mapping=mapping)
							script_wall = time.monotonic() - start
							metrics = extract_metrics(
								script_wall, run.result, args.shots)
							record = {
								"ok": True,
								"circuit_id": cid,
								"submission_path": "backend.run",
								"gate": gate,
								"matching_size": matching_size,
								"matching_index": matching_index,
								"matching_key": edge_key,
								"matching": matching,
								"depth": depth,
								"shots": args.shots,
								"repetition": repetition,
								"qubit_mapping": {
									str(key): value for key, value in mapping.items()
								},
								"job_id": run.job_id,
								"metrics": metrics,
								"expected": parallel_two_q_sequence_model(
									one_q_baselines,
									two_q_baselines,
									gate,
									matching,
									depth,
									execution_per_shot(metrics)),
								"counts": run.counts,
								"files": run.files,
							}
						except Exception as exc:
							record = {
								"ok": False,
								"circuit_id": cid,
								"gate": gate,
								"matching_size": matching_size,
								"matching_index": matching_index,
								"matching_key": edge_key,
								"matching": matching,
								"depth": depth,
								"shots": args.shots,
								"repetition": repetition,
								"qubit_mapping": {
									str(key): value for key, value in mapping.items()
								},
								"error": str(exc),
								"metrics": {},
								"files": {},
							}
						records.append(record)

	write_jsonl(baseline_1q_records_file, baseline_1q_records)
	write_jsonl(baseline_2q_records_file, baseline_2q_records)
	write_jsonl(records_file, records)
	config = {
		"backend": ctx.backend_name,
		"gates": gates,
		"gate_model": "2Q layers with rx/ry 1Q interleaves",
		"matching_sizes": args.matching_sizes,
		"matchings_per_size": args.matchings_per_size,
		"depths": args.depths,
		"shots": args.shots,
		"repetitions": args.repetitions,
		"sample_seed": args.sample_seed,
		"angle": args.angle,
		"dry_run": args.dry_run,
		"coupling_nodes": qhw_coupling_nodes(coupling_graph),
	}
	plots = plot_records(records, plots_dir)
	analysis = build_analysis(records, config, plots)
	ctx.write_json(analysis_file, analysis)
	analysis_md_file.write_text(render_analysis_markdown(analysis))
	summary = {
		"ok": not any(not record.get("ok") for record in records),
		"records": len(records),
		"successful_records": sum(1 for record in records if record.get("ok")),
		"failed_records": sum(1 for record in records if not record.get("ok")),
		"analysis": {
			"status": analysis["overall"]["status"],
			"conclusion": analysis["overall"]["conclusion"],
			"primary_metric": analysis["primary_metric"],
			"plots": plots,
		},
		"files": {
			"backend_info": str(backend_info_file),
			"coupling_graph": str(coupling_graph_file),
			"selected_matchings": str(selected_matchings_file),
			"baseline_1q_records": str(baseline_1q_records_file),
			"baseline_2q_records": str(baseline_2q_records_file),
			"records": str(records_file),
			"analysis_json": str(analysis_file),
			"analysis_markdown": str(analysis_md_file),
			"plots": str(plots_dir),
		},
	}
	ctx.write_json(summary_file, summary)
	summary["files"]["summary"] = str(summary_file)
	return ctx.finish(
		summary,
		ok=summary["ok"],
		text_lines=[
			f"records: {summary['records']}",
			f"successful records: {summary['successful_records']}",
			f"analysis: {analysis['overall']['status']}",
			f"output: {ctx.paths.root}",
		],
	)


if __name__ == "__main__":
	raise SystemExit(main())
