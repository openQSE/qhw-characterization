#!/usr/bin/env python3
"""Measure two-qubit gate timing using Qiskit-authored circuits."""

from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import random
import statistics
import sys
import time
from collections import deque
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
from qhw_util.timing_model import two_q_baseline_key
from qhw_util.timing_model import two_q_baseline_table
from qhw_util.timing_model import two_q_sequence_model
from qhw_util.workflow import WorkflowContext


SUPPORTED_QISKIT_2Q_GATES = ("cz", "cx", "cnot", "swap", "ecr", "rxx", "ryy", "rzz")
PRIMARY_METRIC = "execution_per_shot_seconds"
DIAGNOSTIC_METRICS = (
	"script_wall_seconds",
	"client_total_seconds",
	"server_total_seconds",
)


def dry_run_result(cid: str, shots: int,
		   execution_seconds: float | None = None) -> dict[str, Any]:
	durations = {}
	if execution_seconds is not None:
		durations = {
			"execution_seconds": execution_seconds,
			"provider_total_seconds": execution_seconds,
		}
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
					"num_circuits": 1,
					"counts": {},
					"success": True,
				},
				"timing": {"timestamps": {}, "timeline": [],
					   "durations_seconds": durations},
				"errors": [],
				"extensions": {},
				"raw": {"included": False, "format": None, "artifacts": []},
			},
		},
		"rc": 0,
	}


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


def parse_distance_list(value: str) -> list[str]:
	distances = []
	for raw in value.split(","):
		item = raw.strip().lower()
		if not item:
			continue
		if item in {"far", "max", "disconnected"}:
			distances.append(item)
			continue
		if int(item) < 2:
			raise argparse.ArgumentTypeError(
				"non-connected distance values must be >= 2")
		distances.append(item)
	if not distances:
		raise argparse.ArgumentTypeError("distance list must not be empty")
	return distances


def parse_pair_token(value: str) -> tuple[str, str]:
	for sep in ("-", ":", "/"):
		if sep in value:
			left, right = value.split(sep, 1)
			left = left.strip()
			right = right.strip()
			if left and right:
				return left, right
	raise argparse.ArgumentTypeError(
		f"invalid pair {value!r}; use a form like QB1-QB2")


def parse_pair_list(value: str) -> list[tuple[str, str]]:
	pairs = []
	for raw in value.split(","):
		raw = raw.strip()
		if raw:
			pairs.append(parse_pair_token(raw))
	return pairs


def parse_gate_list(value: str) -> list[str]:
	gates = []
	for raw in value.split(","):
		gate = raw.strip().lower()
		if not gate:
			continue
		if gate not in SUPPORTED_QISKIT_2Q_GATES:
			raise argparse.ArgumentTypeError(
				f"unsupported 2Q gate {gate!r}; supported Qiskit probes are "
				f"{', '.join(SUPPORTED_QISKIT_2Q_GATES)}")
		gates.append(gate)
	if not gates:
		raise argparse.ArgumentTypeError("gate list must not be empty")
	return gates


def parse_angle(value: str) -> float:
	allowed = {"pi": math.pi}
	try:
		return float(eval(value, {"__builtins__": {}}, allowed))
	except Exception as exc:
		raise argparse.ArgumentTypeError(
			f"invalid angle expression {value!r}") from exc


def canonical_pair(pair: tuple[str, str] | list[str]) -> tuple[str, str]:
	left, right = str(pair[0]), str(pair[1])
	return tuple(sorted((left, right)))


def ordered_pair_key(pair: tuple[str, str] | list[str]) -> str:
	return f"{pair[0]}-{pair[1]}"


def canonical_pair_key(pair: tuple[str, str] | list[str]) -> str:
	left, right = canonical_pair(pair)
	return f"{left}-{right}"


def synthetic_coupling_graph() -> dict[str, Any]:
	nodes = [f"QB{index}" for index in range(1, 7)]
	edges = [
		["QB1", "QB2"],
		["QB2", "QB3"],
		["QB3", "QB4"],
		["QB4", "QB5"],
		["QB5", "QB6"],
	]
	return {
		"schema": "qhw-coupling-v1",
		"provider": "dry-run",
		"device": {"id": "dry-run", "provider": "dry-run", "num_qubits": 6},
		"coupling": {
			"directed": False,
			"nodes": nodes,
			"edges": edges,
			"source": ["synthetic.dry_run"],
		},
		"operations": [{
			"name": "cz",
			"native_name": "cz",
			"arity": 2,
			"supported_loci": edges,
		}],
	}


def graph_adjacency(nodes: list[str], edges: list[list[str]]) -> dict[str, set[str]]:
	adjacency = {node: set() for node in nodes}
	for left, right in edges:
		adjacency.setdefault(left, set()).add(right)
		adjacency.setdefault(right, set()).add(left)
	return adjacency


def shortest_path(adjacency: dict[str, set[str]], src: str,
		  dst: str) -> list[str] | None:
	if src == dst:
		return [src]
	visited = {src}
	queue = deque([[src]])
	while queue:
		path = queue.popleft()
		for neighbor in sorted(adjacency.get(path[-1], ())):
			if neighbor in visited:
				continue
			next_path = path + [neighbor]
			if neighbor == dst:
				return next_path
			visited.add(neighbor)
			queue.append(next_path)
	return None


def operation_loci_by_gate(coupling_graph: dict[str, Any]) -> dict[str, list[tuple[str, str]]]:
	result: dict[str, list[tuple[str, str]]] = {}
	for operation in coupling_graph.get("operations") or []:
		if int(operation.get("arity", 0)) != 2:
			continue
		name = str(operation.get("name") or "").lower()
		if not name:
			continue
		loci = []
		for locus in operation.get("supported_loci") or []:
			if len(locus) == 2:
				loci.append((str(locus[0]), str(locus[1])))
		if loci:
			result[name] = loci
	return result


def resolve_gates(value: str, coupling_graph: dict[str, Any]) -> list[str]:
	if value != "auto":
		return parse_gate_list(value)
	ops = operation_loci_by_gate(coupling_graph)
	candidates = [
		gate for gate in ops
		if gate in SUPPORTED_QISKIT_2Q_GATES
	]
	if "cz" in candidates:
		return ["cz"]
	if candidates:
		return [candidates[0]]
	return ["cz"]


def connected_pairs_for_gate(gate: str, coupling_graph: dict[str, Any],
			     requested: str, max_pairs: int,
			     sample_seed: int) -> list[dict[str, Any]]:
	edges = [tuple(edge) for edge in qhw_coupling_edges(coupling_graph)]
	op_loci = operation_loci_by_gate(coupling_graph).get(gate, [])
	if requested == "all":
		source = op_loci or edges
	else:
		source = parse_pair_list(requested)
	edge_set = {canonical_pair(edge) for edge in edges}
	seen = set()
	records = []
	for pair in source:
		key = canonical_pair(pair)
		if key in seen:
			continue
		seen.add(key)
		records.append({
			"pair": [pair[0], pair[1]],
			"pair_kind": "connected",
			"graph_distance": 1,
			"shortest_path": [pair[0], pair[1]],
			"native_locus_supported": key in edge_set,
			"selection_source": "operation_supported_loci"
			if requested == "all" and op_loci else "coupling_graph",
		})
	if max_pairs > 0 and len(records) > max_pairs:
		rng = random.Random(sample_seed)
		records = sorted(rng.sample(records, max_pairs),
				 key=lambda item: canonical_pair_key(item["pair"]))
	return records


def non_connected_pairs(coupling_graph: dict[str, Any],
			distances: list[str],
			per_distance: int,
			sample_seed: int,
			max_pairs: int) -> list[dict[str, Any]]:
	if per_distance < 1:
		return []

	nodes = qhw_coupling_nodes(coupling_graph)
	edges = qhw_coupling_edges(coupling_graph)
	edge_set = {canonical_pair(edge) for edge in edges}
	adjacency = graph_adjacency(nodes, edges)
	by_distance: dict[str, list[dict[str, Any]]] = {}
	for left, right in itertools.combinations(nodes, 2):
		if canonical_pair((left, right)) in edge_set:
			continue
		path = shortest_path(adjacency, left, right)
		if path is None:
			distance_key = "disconnected"
			graph_distance = None
		else:
			graph_distance = len(path) - 1
			distance_key = str(graph_distance)
		by_distance.setdefault(distance_key, []).append({
			"pair": [left, right],
			"pair_kind": "non_connected",
			"graph_distance": graph_distance,
			"shortest_path": path,
			"native_locus_supported": False,
			"selection_source": f"distance_{distance_key}",
		})

	finite_distances = [
		int(key) for key in by_distance
		if key.isdigit()
	]
	far_key = str(max(finite_distances)) if finite_distances else None
	resolved_distances = []
	for item in distances:
		if item in {"far", "max"}:
			if far_key:
				resolved_distances.append(far_key)
		else:
			resolved_distances.append(item)

	selected = []
	rng = random.Random(sample_seed)
	for distance in resolved_distances:
		candidates = list(by_distance.get(distance, []))
		if not candidates:
			continue
		candidates = sorted(
			candidates, key=lambda item: canonical_pair_key(item["pair"]))
		if len(candidates) > per_distance:
			candidates = sorted(
				rng.sample(candidates, per_distance),
				key=lambda item: canonical_pair_key(item["pair"]))
		selected.extend(candidates)

	seen = set()
	unique = []
	for item in selected:
		key = canonical_pair(item["pair"])
		if key in seen:
			continue
		seen.add(key)
		unique.append(item)
	if max_pairs > 0 and len(unique) > max_pairs:
		unique = sorted(
			rng.sample(unique, max_pairs),
			key=lambda item: (
				item["graph_distance"] if item["graph_distance"] is not None else 9999,
				canonical_pair_key(item["pair"])))
	return unique


def apply_1q_gate(circuit, gate: str, qubit: int, angle: float) -> None:
	if gate == "x":
		circuit.x(qubit)
	elif gate == "rx":
		circuit.rx(angle, qubit)
	elif gate == "ry":
		circuit.ry(angle, qubit)
	else:
		raise ValueError(f"unsupported 1Q gate {gate!r}")


def apply_2q_gate(circuit, gate: str, left: int, right: int, angle: float) -> None:
	if gate == "cz":
		circuit.cz(left, right)
	elif gate in {"cx", "cnot"}:
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
		raise ValueError(f"unsupported 2Q gate {gate!r}")


def build_single_1q_circuit(gate: str, angle: float, name: str):
	try:
		from qiskit import QuantumCircuit
	except Exception as exc:
		raise RuntimeError(
			"qiskit is required for timing_2q.py") from exc
	circuit = QuantumCircuit(1, 1, name=name)
	apply_1q_gate(circuit, gate, 0, angle)
	circuit.measure(0, 0)
	return circuit


def build_single_2q_circuit(gate: str, angle: float, name: str):
	try:
		from qiskit import QuantumCircuit
	except Exception as exc:
		raise RuntimeError(
			"qiskit is required for timing_2q.py") from exc
	circuit = QuantumCircuit(2, 2, name=name)
	apply_2q_gate(circuit, gate, 0, 1, angle)
	circuit.measure([0, 1], [0, 1])
	return circuit


def build_gate_circuit(gate: str, depth: int, angle: float, name: str):
	try:
		from qiskit import QuantumCircuit
	except Exception as exc:
		raise RuntimeError(
			"qiskit is required for timing_2q.py") from exc

	circuit = QuantumCircuit(2, 2, name=name)
	for _ in range(depth):
		apply_1q_gate(circuit, "rx", 0, angle)
		apply_1q_gate(circuit, "rx", 1, angle)
		apply_2q_gate(circuit, gate, 0, 1, angle)
		apply_1q_gate(circuit, "ry", 0, angle)
		apply_1q_gate(circuit, "ry", 1, angle)
	circuit.measure([0, 1], [0, 1])
	return circuit


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


def dry_run_2q_sequence_execution_seconds(gate: str, depth: int,
					  shots: int) -> float:
	per_shot = (
		2 * dry_run_1q_execution_seconds("rx", 1)
		+ dry_run_2q_execution_seconds(gate, 1)
		+ 2 * dry_run_1q_execution_seconds("ry", 1)
	)
	return shots * depth * per_shot


def extract_metrics(script_wall_seconds: float, result: dict[str, Any],
		    shots: int) -> dict[str, float | None]:
	qhw_result = backend_result_qhw(result)
	if not qhw_result:
		raise ValueError("backend result did not include normalized qhw_result")
	timing = qhw_result.get("timing", {})
	durations = timing.get("durations_seconds", {})
	server_total = durations.get("provider_total_seconds")
	execution = durations.get("execution_seconds")
	return {
		"script_wall_seconds": script_wall_seconds,
		"client_total_seconds": None,
		"server_total_seconds": server_total,
		"execution_seconds": execution,
		"server_total_per_shot_seconds": (
			server_total / shots if server_total is not None else None),
		"execution_per_shot_seconds": (
			execution / shots if execution is not None else None),
	}


def result_job_ids(result: dict[str, Any]) -> list[str]:
	qhw_result = backend_result_qhw(result)
	if not qhw_result:
		raise ValueError("backend result did not include normalized qhw_result")
	job_id = (qhw_result.get("job", {}) or {}).get("id")
	if job_id:
		return [str(job_id)]
	return []


def safe_float(value: Any) -> float | None:
	if value is None:
		return None
	try:
		return float(value)
	except (TypeError, ValueError):
		return None


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
		"slope_seconds_per_gate": slope,
		"rms_residual_seconds": rms,
		"points": n,
		"depth_min": min(xs),
		"depth_max": max(xs),
		"y_mean": statistics.fmean(ys),
	}


def metric_points(records: list[dict[str, Any]],
		  metric: str = PRIMARY_METRIC) -> list[tuple[float, float]]:
	points = []
	for record in records:
		if not record.get("ok"):
			continue
		value = safe_float(record.get("metrics", {}).get(metric))
		if value is None:
			continue
		points.append((float(record["depth"]), value))
	return sorted(points)


def group_key(record: dict[str, Any], key: str) -> str:
	if key == "gate_pair":
		return f"{record['gate']}:{record['physical_pair_key']}"
	if key == "gate_pair_kind":
		return f"{record['gate']}:{record['pair_kind']}"
	if key == "pair_kind":
		return record["pair_kind"]
	if key == "gate":
		return record["gate"]
	raise ValueError(f"unsupported group key {key!r}")


def group_records(records: list[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
	grouped: dict[str, list[dict[str, Any]]] = {}
	for record in records:
		if not record.get("ok"):
			continue
		grouped.setdefault(group_key(record, key), []).append(record)
	return grouped


def classify_fit(fit: dict[str, Any] | None, pair_kind: str | None = None) -> dict[str, Any]:
	if not fit:
		return {
			"status": "insufficient_data",
			"conclusion": (
				"Fewer than two successful records contain hardware execution "
				"timing, so this group cannot answer the 2Q depth-scaling "
				"question."),
		}
	depth_span = fit["depth_max"] - fit["depth_min"]
	predicted_delta = fit["slope_seconds_per_gate"] * depth_span
	mean = max(abs(fit["y_mean"]), 1e-15)
	delta_fraction = abs(predicted_delta) / mean
	rms = fit["rms_residual_seconds"]
	residual_reference = max(abs(predicted_delta), mean, 1e-15)
	residual_fraction = rms / residual_reference

	if delta_fraction < 0.05:
		status = "no_depth_dependence_observed"
		conclusion = (
			"The execution-time data does not show a clear depth-dependent "
			"increase for this two-qubit group.")
	elif fit["slope_seconds_per_gate"] > 0 and residual_fraction <= 0.2:
		status = "approximately_linear_positive"
		conclusion = (
			"Hardware execution time per shot increases approximately "
			"linearly with repeated two-qubit layer depth for this group.")
	elif fit["slope_seconds_per_gate"] > 0:
		status = "positive_but_noisy"
		conclusion = (
			"Hardware execution time increases with depth, but the fit is "
			"noisy enough that repeated runs are needed before using the "
			"slope as a stable per-layer estimate.")
	else:
		status = "not_linear_positive"
		conclusion = (
			"The fitted slope is not positive, so this run does not support "
			"a linear 2Q per-layer timing model for this group.")

	if pair_kind == "non_connected":
		conclusion += (
			" This group uses non-connected physical pairs, so successful "
			"runs measure routed or provider-handled behavior rather than "
			"native two-qubit gate duration.")

	return {
		"status": status,
		"conclusion": conclusion,
		"predicted_delta_seconds": predicted_delta,
		"delta_fraction_of_mean": delta_fraction,
		"residual_fraction": residual_fraction,
	}


def analyze_group(records: list[dict[str, Any]]) -> dict[str, Any]:
	points = metric_points(records, PRIMARY_METRIC)
	fit = linear_fit(points)
	pair_kinds = {record["pair_kind"] for record in records}
	pair_kind = next(iter(pair_kinds)) if len(pair_kinds) == 1 else None
	return {
		"record_count": len(records),
		"valid_primary_metric_count": len(points),
		"depths": sorted({record["depth"] for record in records}),
		"fit": fit,
		"classification": classify_fit(fit, pair_kind),
	}


def build_analysis(records: list[dict[str, Any]],
		   config: dict[str, Any],
		   plots: dict[str, Any]) -> dict[str, Any]:
	by_gate_pair = {
		key: analyze_group(group)
		for key, group in sorted(group_records(records, "gate_pair").items())
	}
	by_gate_pair_kind = {
		key: analyze_group(group)
		for key, group in sorted(group_records(records, "gate_pair_kind").items())
	}
	status_counts: dict[str, int] = {}
	for item in by_gate_pair.values():
		status = item["classification"]["status"]
		status_counts[status] = status_counts.get(status, 0) + 1
	valid_count = sum(
		1 for record in records
		if record.get("ok")
		and safe_float(record.get("metrics", {}).get(PRIMARY_METRIC)) is not None)
	connected_failures = [
		record for record in records
		if record["pair_kind"] == "connected" and not record.get("ok")
	]
	non_connected_successes = [
		record for record in records
		if record["pair_kind"] == "non_connected" and record.get("ok")
	]
	non_connected_failures = [
		record for record in records
		if record["pair_kind"] == "non_connected" and not record.get("ok")
	]

	if connected_failures:
		overall_status = "connected_pair_failures"
		overall_conclusion = (
			"At least one connected two-qubit pair failed. This should be "
			"investigated before using the timing data as native 2Q "
			"characterization.")
	elif valid_count == 0:
		overall_status = "no_hardware_execution_timing"
		overall_conclusion = (
			"No successful record contained hardware execution timing.")
	elif status_counts.get("approximately_linear_positive", 0):
		overall_status = "depth_scaling_observed"
		overall_conclusion = (
			"At least one two-qubit gate/pair group shows approximately "
			"linear increase in hardware execution time per shot as depth "
			"increases.")
	else:
		overall_status = "depth_scaling_not_established"
		overall_conclusion = (
			"This run did not establish a clean positive linear "
			"depth-scaling trend in the hardware execution-time metric.")

	return {
		"schema": "qhw-2q-analysis-v1",
		"intent": (
			"Determine whether hardware execution time increases linearly "
			"as repeated two-qubit gate layers with 1Q interleaves are "
			"applied to connected physical pairs, and compare that behavior "
			"with sampled non-connected pairs."),
		"primary_metric": PRIMARY_METRIC,
		"primary_metric_definition": (
			"Provider timeline execution duration divided by shots. The "
			"execution duration is the interval from execution_started to "
			"execution_ended in the normalized qhw result timeline."),
		"diagnostic_metrics_not_used_for_hardware_conclusion": list(
			DIAGNOSTIC_METRICS),
		"config": config,
		"record_count": len(records),
		"successful_record_count": sum(
			1 for record in records if record.get("ok")),
		"valid_primary_metric_count": valid_count,
		"failed_record_count": sum(
			1 for record in records if not record.get("ok")),
		"connected_failure_count": len(connected_failures),
		"non_connected_success_count": len(non_connected_successes),
		"non_connected_failure_count": len(non_connected_failures),
		"overall": {
			"status": overall_status,
			"conclusion": overall_conclusion,
			"gate_pair_status_counts": status_counts,
		},
		"by_gate_pair": by_gate_pair,
		"by_gate_pair_kind": by_gate_pair_kind,
		"expected_model_summary": expected_model_summary(records),
		"plots": plots,
		"caveats": [
			"The script inserts rx/ry 1Q layers around each 2Q layer to "
			"reduce repeated-gate cancellation opportunities.",
			"Connected pairs estimate native or provider-supported 2Q gate "
			"timing. Non-connected pairs measure routed/provider-handled "
			"behavior when they succeed.",
			"Client wall time and server total time include non-hardware "
			"overheads and are not used for the primary hardware timing "
			"conclusion.",
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

	def y(record):
		return safe_float(record["metrics"].get(PRIMARY_METRIC))

	def write_plot(name: str):
		path = plots_dir / name
		plt.tight_layout()
		plt.savefig(path, dpi=150)
		plt.close()
		files.append(str(path))

	for gate, gate_records in sorted(group_records(valid, "gate").items()):
		plt.figure(figsize=(9, 5))
		for pair_kind, group in sorted(group_records(
				gate_records, "pair_kind").items()):
			aggregate: dict[int, list[float]] = {}
			for record in group:
				value = y(record)
				if value is None:
					continue
				aggregate.setdefault(record["depth"], []).append(value)
			points = sorted(
				(depth, statistics.fmean(values))
				for depth, values in aggregate.items())
			if not points:
				continue
			plt.plot(
				[point[0] for point in points],
				[point[1] for point in points],
				marker="o",
				label=pair_kind)
		plt.xlabel("Repeated 2Q gate depth")
		plt.ylabel("Mean hardware execution time per shot (s)")
		plt.title(f"2Q {gate} timing by pair kind")
		plt.xscale("log", base=2)
		plt.grid(True, which="both", alpha=0.3)
		plt.legend(fontsize="small")
		write_plot(f"2q_{gate}_pair_kind_execution_per_shot.png")

	for gate, gate_records in sorted(group_records(valid, "gate").items()):
		connected = [
			record for record in gate_records
			if record["pair_kind"] == "connected"
		]
		if not connected:
			continue
		plt.figure(figsize=(10, 6))
		for pair_key, group in sorted(group_records(connected, "gate_pair").items()):
			points = sorted((record["depth"], y(record))
					for record in group if y(record) is not None)
			if not points:
				continue
			plt.plot(
				[point[0] for point in points],
				[point[1] for point in points],
				marker="o",
				label=pair_key.split(":", 1)[1])
		plt.xlabel("Repeated 2Q gate depth")
		plt.ylabel("Hardware execution time per shot (s)")
		plt.title(f"2Q {gate} connected-pair timing")
		plt.xscale("log", base=2)
		plt.grid(True, which="both", alpha=0.3)
		if len({record["physical_pair_key"] for record in connected}) <= 16:
			plt.legend(fontsize="x-small")
		write_plot(f"2q_{gate}_connected_pairs_execution_per_shot.png")

	for gate, gate_records in sorted(group_records(valid, "gate").items()):
		plt.figure(figsize=(10, 6))
		plotted = 0
		for pair_key, group in sorted(group_records(
				gate_records, "gate_pair").items()):
			points = metric_points(group, PRIMARY_METRIC)
			fit = linear_fit(points)
			if not fit:
				continue
			residuals = [
				(depth, value - (
					fit["intercept_seconds"]
					+ fit["slope_seconds_per_gate"] * depth))
				for depth, value in points
			]
			pair_kind = group[0].get("pair_kind", "unknown")
			label = f"{pair_key.split(':', 1)[1]} ({pair_kind})"
			plt.plot(
				[point[0] for point in residuals],
				[point[1] for point in residuals],
				marker="o",
				label=label)
			plotted += 1
		plt.axhline(0, color="black", linewidth=1)
		plt.xlabel("Repeated 2Q gate depth")
		plt.ylabel("Residual execution time per shot (s)")
		plt.title(f"2Q {gate} fit residuals")
		plt.xscale("log", base=2)
		plt.grid(True, which="both", alpha=0.3)
		if plotted <= 16:
			plt.legend(fontsize="x-small")
		if plotted:
			write_plot(f"2q_{gate}_fit_residuals.png")
		else:
			plt.close()

	slope_rows = []
	labels = []
	for key, group in sorted(group_records(valid, "gate_pair").items()):
		fit = linear_fit(metric_points(group, PRIMARY_METRIC))
		if not fit:
			continue
		labels.append(key)
		slope_rows.append([fit["slope_seconds_per_gate"]])
	if slope_rows:
		plt.figure(figsize=(6, max(4, len(labels) * 0.25)))
		image = plt.imshow(slope_rows, aspect="auto")
		plt.colorbar(image, label="Slope (s/depth/shot)")
		plt.xticks([0], ["slope"])
		plt.yticks(range(len(labels)), labels, fontsize="x-small")
		plt.title("2Q fitted execution-time slope")
		write_plot("2q_gate_pair_slope_heatmap.png")

	return {
		"status": "generated",
		"metric": PRIMARY_METRIC,
		"files": files,
	}


def render_analysis_markdown(analysis: dict[str, Any]) -> str:
	lines = [
		"# 2Q Timing Analysis",
		"",
		"## Intent",
		"",
		analysis["intent"],
		"",
		"## Primary Metric",
		"",
		f"`{analysis['primary_metric']}` is used for the hardware timing "
		"conclusion.",
		"",
		analysis["primary_metric_definition"],
		"",
		"The following diagnostic metrics are recorded but are not used for "
		"the hardware conclusion:",
		"",
	]
	for metric in analysis["diagnostic_metrics_not_used_for_hardware_conclusion"]:
		lines.append(f"- `{metric}`")

	lines += [
		"",
		"## Overall Result",
		"",
		f"Status: `{analysis['overall']['status']}`",
		"",
		analysis["overall"]["conclusion"],
		"",
		f"Records: {analysis['record_count']}",
		f"Successful records: {analysis['successful_record_count']}",
		f"Records with hardware execution timing: "
		f"{analysis['valid_primary_metric_count']}",
		f"Failed records: {analysis['failed_record_count']}",
		f"Connected-pair failures: {analysis['connected_failure_count']}",
		f"Non-connected successes: {analysis['non_connected_success_count']}",
		f"Non-connected failures: {analysis['non_connected_failure_count']}",
		"",
		"## Gate/Pair Fits",
		"",
		"| Gate/Pair | Status | Points | Slope (s/depth/shot) | RMS residual (s) | Conclusion |",
		"| --- | --- | ---: | ---: | ---: | --- |",
	]
	for key, item in analysis["by_gate_pair"].items():
		fit = item.get("fit") or {}
		classification = item["classification"]
		lines.append(
			f"| `{key}` | `{classification['status']}` | "
			f"{item['valid_primary_metric_count']} | "
			f"{fit.get('slope_seconds_per_gate', '')} | "
			f"{fit.get('rms_residual_seconds', '')} | "
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

	lines += [
		"",
		"## Plots",
		"",
	]
	plots = analysis.get("plots", {})
	if plots.get("status") == "generated":
		for path in plots.get("files", []):
			lines.append(f"- `{path}`")
	else:
		lines.append(
			f"Plot generation was skipped: {plots.get('reason', 'unknown')}")

	lines += [
		"",
		"## Caveats",
		"",
	]
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
	parser.add_argument("--gates", default="auto")
	parser.add_argument("--connected-pairs", default="all")
	parser.add_argument("--max-connected-pairs", type=int, default=0,
			    help="Maximum connected pairs to sample; 0 means all.")
	parser.add_argument("--non-connected", choices=("sample", "none", "all"),
			    default="sample")
	parser.add_argument("--non-connected-distances", type=parse_distance_list,
			    default=parse_distance_list("2,3,far"))
	parser.add_argument("--non-connected-per-distance", type=int, default=4)
	parser.add_argument("--max-non-connected-pairs", type=int, default=0,
			    help="Maximum sampled non-connected pairs; 0 means no cap.")
	parser.add_argument("--sample-seed", type=int, default=1)
	parser.add_argument("--depths", type=parse_int_list,
			    default=parse_int_list("1,2,4,8,16,32,64,128"))
	parser.add_argument("--shots", type=int, default=100)
	parser.add_argument("--repetitions", type=int, default=1)
	parser.add_argument("--angle", type=parse_angle, default=math.pi)
	parser.add_argument("--require-non-connected-success", action="store_true")


def main() -> int:
	ctx = WorkflowContext.from_cli(
		__file__,
		description=(
			"Measure two-qubit gate timing on connected and sampled "
			"non-connected physical pairs."),
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
	if args.non_connected_per_distance < 1:
		raise ValueError("--non-connected-per-distance must be at least 1")

	backend_info = {} if args.dry_run else to_jsonable(
		ctx.backend.get_backend_info())
	coupling_graph = synthetic_coupling_graph() if args.dry_run else to_jsonable(
		ctx.backend.get_coupling_graph(args.calibration_set_id))
	gates = resolve_gates(args.gates, coupling_graph)
	connected_by_gate = {
		gate: connected_pairs_for_gate(
			gate,
			coupling_graph,
			args.connected_pairs,
			args.max_connected_pairs,
			args.sample_seed)
		for gate in gates
	}
	if args.non_connected == "none":
		non_connected = []
	else:
		non_connected_distances = (
			[str(distance) for distance in range(
				2, len(qhw_coupling_nodes(coupling_graph)))]
			+ ["disconnected"]
			if args.non_connected == "all"
			else args.non_connected_distances)
		max_non_connected = (
			0 if args.non_connected == "all"
			else args.max_non_connected_pairs)
		per_distance = (
			10**9 if args.non_connected == "all"
			else args.non_connected_per_distance)
		non_connected = non_connected_pairs(
			coupling_graph,
			non_connected_distances,
			per_distance,
			args.sample_seed,
			max_non_connected)

	backend_info_file = ctx.paths.root / "backend_info.json"
	coupling_graph_file = qhw_json_path(ctx.paths.root, "coupling_graph")
	selected_pairs_file = ctx.paths.root / "selected_pairs.json"
	baseline_1q_records_file = ctx.paths.results / "baseline_1q_records.jsonl"
	baseline_2q_records_file = ctx.paths.results / "baseline_2q_records.jsonl"
	records_file = ctx.paths.results / "timing_records.jsonl"
	summary_file = ctx.paths.results / "timing_summary.json"
	analysis_file = ctx.paths.results / "analysis.json"
	analysis_md_file = ctx.paths.results / "analysis.md"
	plots_dir = ctx.paths.results / "plots"
	ctx.write_json(backend_info_file, backend_info)
	ctx.write_json(coupling_graph_file, coupling_graph)

	selected_pairs = {
		"gates": gates,
		"connected_by_gate": connected_by_gate,
		"non_connected": non_connected,
	}
	ctx.write_json(selected_pairs_file, selected_pairs)

	pair_specs_by_gate = {}
	for gate in gates:
		pair_specs_by_gate[gate] = [
			{**item, "gate": gate}
			for item in connected_by_gate[gate]
		] + [
			{**item, "gate": gate}
			for item in non_connected
		]
	involved_qubits = sorted({
		str(qubit)
		for pair_specs in pair_specs_by_gate.values()
		for pair_spec in pair_specs
		for qubit in pair_spec["pair"]
	})

	baseline_1q_records = []
	for repetition in range(args.repetitions):
		for qubit in involved_qubits:
			for baseline_gate in ("rx", "ry"):
				cid = (
					f"baseline_1q_{qubit}_{baseline_gate}_"
					f"s{args.shots}_r{repetition}")
				circuit = build_single_1q_circuit(
					baseline_gate, args.angle, cid)
				start = time.monotonic()
				try:
					if args.dry_run:
						qasm_files = ctx.write_qasm_artifacts(
							[circuit], cid)
						result = dry_run_result(
							cid,
							args.shots,
							dry_run_1q_execution_seconds(
								baseline_gate, args.shots))
						run = ctx.write_backend_result(
							cid, result, qasm_files)
					else:
						run = ctx.run_circuit(
							[circuit],
							name=cid,
							shots=args.shots,
							qubit_mapping={0: qubit},
						)
						result = run.result
					ok = run.ok
					error = None
				except Exception as exc:
					result = {"rc": 1, "error": str(exc)}
					run = None
					ok = False
					error = str(exc)
				wall = time.monotonic() - start
				qhw_result = backend_result_qhw(result)
				qhw_payload = qhw_result.get("result", {})
				baseline_1q_records.append({
					"experiment": "two_qubit_timing_1q_baseline",
					"ok": ok,
					"error": error,
					"repetition": repetition,
					"physical_qubit": qubit,
					"logical_qubits": 1,
					"gate": baseline_gate,
					"angle_radians": args.angle,
					"depth": 1,
					"shots": args.shots,
					"backend_mode": args.backend if args.dry_run
					else ctx.backend.name,
					"source": "qiskit",
					"submission_path": "backend.run",
					"qubit_mapping": {"0": qubit},
					"qasm_file": run.files.get("qasm") if run else None,
					"result_file": run.files.get("result") if run else None,
					"raw_result_file": run.files.get("raw_result") if run else None,
					"normalized_result_file": (
						run.files.get("normalized_result") if run else None),
					"job_ids": result_job_ids(result) if run else [],
					"counts": qhw_payload.get("counts")
					if isinstance(qhw_payload, dict) else None,
					"metrics": extract_metrics(
						wall, result, args.shots) if run else {},
				})

	baseline_2q_records = []
	for repetition in range(args.repetitions):
		for gate, pair_specs in pair_specs_by_gate.items():
			for pair_spec in pair_specs:
				pair = tuple(pair_spec["pair"])
				cid = (
					f"baseline_2q_{gate}_{ordered_pair_key(pair)}_"
					f"s{args.shots}_r{repetition}")
				circuit = build_single_2q_circuit(gate, args.angle, cid)
				qubit_mapping = {0: pair[0], 1: pair[1]}
				start = time.monotonic()
				try:
					if args.dry_run:
						qasm_files = ctx.write_qasm_artifacts(
							[circuit], cid)
						result = dry_run_result(
							cid,
							args.shots,
							dry_run_2q_execution_seconds(
								gate, args.shots))
						run = ctx.write_backend_result(
							cid, result, qasm_files)
					else:
						run = ctx.run_circuit(
							[circuit],
							name=cid,
							shots=args.shots,
							qubit_mapping=qubit_mapping,
						)
						result = run.result
					ok = run.ok
					error = None
				except Exception as exc:
					result = {"rc": 1, "error": str(exc)}
					run = None
					ok = False
					error = str(exc)
				wall = time.monotonic() - start
				qhw_result = backend_result_qhw(result)
				qhw_payload = qhw_result.get("result", {})
				baseline_2q_records.append({
					"experiment": "two_qubit_gate_baseline",
					"baseline_key": two_q_baseline_key(gate, pair),
					"ok": ok,
					"error": error,
					"repetition": repetition,
					"pair_kind": pair_spec["pair_kind"],
					"physical_pair": list(pair),
					"physical_pair_key": ordered_pair_key(pair),
					"canonical_pair_key": canonical_pair_key(pair),
					"logical_qubits": 2,
					"gate": gate,
					"angle_radians": args.angle,
					"depth": 1,
					"shots": args.shots,
					"backend_mode": args.backend if args.dry_run
					else ctx.backend.name,
					"source": "qiskit",
					"submission_path": "backend.run",
					"qubit_mapping": {"0": pair[0], "1": pair[1]},
					"qasm_file": run.files.get("qasm") if run else None,
					"result_file": run.files.get("result") if run else None,
					"raw_result_file": run.files.get("raw_result") if run else None,
					"normalized_result_file": (
						run.files.get("normalized_result") if run else None),
					"job_ids": result_job_ids(result) if run else [],
					"counts": qhw_payload.get("counts")
					if isinstance(qhw_payload, dict) else None,
					"metrics": extract_metrics(
						wall, result, args.shots) if run else {},
				})

	one_q_baselines = one_q_baseline_table(baseline_1q_records)
	two_q_baselines = two_q_baseline_table(baseline_2q_records)

	records = []
	for repetition in range(args.repetitions):
		for gate, pair_specs in pair_specs_by_gate.items():
			for pair_spec in pair_specs:
				pair = tuple(pair_spec["pair"])
				for depth in args.depths:
					cid = (
						f"2q_{gate}_{ordered_pair_key(pair)}_"
						f"d{depth}_s{args.shots}_r{repetition}")
					circuit = build_gate_circuit(
						gate, depth, args.angle, cid)
					qubit_mapping = {0: pair[0], 1: pair[1]}

					start = time.monotonic()
					try:
						if args.dry_run:
							qasm_files = ctx.write_qasm_artifacts(
								[circuit], cid)
							result = dry_run_result(
								cid,
								args.shots,
								dry_run_2q_sequence_execution_seconds(
									gate, depth, args.shots))
							run = ctx.write_backend_result(
								cid, result, qasm_files)
						else:
							run = ctx.run_circuit(
								[circuit],
								name=cid,
								shots=args.shots,
								qubit_mapping=qubit_mapping,
							)
							result = run.result
						ok = run.ok
						error = None
					except Exception as exc:
						result = {"rc": 1, "error": str(exc)}
						run = None
						ok = False
						error = str(exc)
					wall = time.monotonic() - start
					qhw_result = backend_result_qhw(result)
					qhw_payload = qhw_result.get("result", {})
					metrics = extract_metrics(
						wall, result, args.shots) if run else {}

					records.append({
						"experiment": "two_qubit_gate_timing",
						"ok": ok,
						"error": error,
						"repetition": repetition,
						"pair_kind": pair_spec["pair_kind"],
						"physical_pair": list(pair),
						"physical_pair_key": ordered_pair_key(pair),
						"canonical_pair_key": canonical_pair_key(pair),
						"graph_distance": pair_spec["graph_distance"],
						"shortest_path": pair_spec["shortest_path"],
						"native_locus_supported": (
							pair_spec["native_locus_supported"]),
						"selection_source": pair_spec["selection_source"],
						"logical_qubits": 2,
						"gate": gate,
						"angle_radians": args.angle,
						"depth": depth,
						"shots": args.shots,
						"backend_mode": args.backend if args.dry_run
						else ctx.backend.name,
						"source": "qiskit",
						"submission_path": "backend.run",
						"qubit_mapping": {"0": pair[0], "1": pair[1]},
						"qasm_file": run.files.get("qasm") if run else None,
						"result_file": run.files.get("result") if run else None,
						"raw_result_file": run.files.get("raw_result") if run else None,
						"normalized_result_file": (
							run.files.get("normalized_result") if run else None),
						"job_ids": result_job_ids(result) if run else [],
						"counts": qhw_payload.get("counts")
						if isinstance(qhw_payload, dict) else None,
						"metrics": metrics,
						"expected": two_q_sequence_model(
							one_q_baselines,
							two_q_baselines,
							gate,
							pair,
							depth,
							execution_per_shot(metrics)),
					})

	write_jsonl(baseline_1q_records_file, baseline_1q_records)
	write_jsonl(baseline_2q_records_file, baseline_2q_records)
	write_jsonl(records_file, records)
	config = {
		"gates": gates,
		"gate_model": (
			"Qiskit 2Q gates with rx/ry 1Q interleaves and explicit "
			"logical-to-physical mapping"),
		"depths": args.depths,
		"shots": args.shots,
		"repetitions": args.repetitions,
		"angle_radians": args.angle,
		"calibration_set_id": args.calibration_set_id,
		"connected_pairs": args.connected_pairs,
		"max_connected_pairs": args.max_connected_pairs,
		"non_connected": args.non_connected,
		"non_connected_distances": args.non_connected_distances,
		"non_connected_per_distance": args.non_connected_per_distance,
		"max_non_connected_pairs": args.max_non_connected_pairs,
		"sample_seed": args.sample_seed,
	}
	plots = plot_records(records, plots_dir)
	analysis = build_analysis(records, config, plots)
	ctx.write_json(analysis_file, analysis)
	analysis_md_file.write_text(render_analysis_markdown(analysis))

	connected_failures = [
		record for record in records
		if record["pair_kind"] == "connected" and not record["ok"]
	]
	non_connected_failures = [
		record for record in records
		if record["pair_kind"] == "non_connected" and not record["ok"]
	]
	ok = not connected_failures and (
		not args.require_non_connected_success or not non_connected_failures)
	summary = {
		"ok": ok,
		"run_id": ctx.paths.run_id,
		"date_id": ctx.paths.date_id,
		"output_dir": str(ctx.paths.root),
		"backend_mode": ctx.backend_name,
		"dry_run": args.dry_run,
		"config": config,
		"record_count": len(records),
		"failed_record_count": sum(
			1 for record in records if not record["ok"]),
		"connected_failure_count": len(connected_failures),
		"non_connected_failure_count": len(non_connected_failures),
		"analysis": {
			"status": analysis["overall"]["status"],
			"conclusion": analysis["overall"]["conclusion"],
			"primary_metric": analysis["primary_metric"],
			"plots": plots,
		},
		"files": {
			"backend_info": str(backend_info_file),
			"coupling_graph": str(coupling_graph_file),
			"selected_pairs": str(selected_pairs_file),
			"baseline_1q_records": str(baseline_1q_records_file),
			"baseline_2q_records": str(baseline_2q_records_file),
			"timing_records": str(records_file),
			"timing_summary": str(summary_file),
			"analysis_json": str(analysis_file),
			"analysis_markdown": str(analysis_md_file),
			"plots": str(plots_dir),
		},
	}
	summary["files"]["script_output"] = str(ctx.script_output_file)
	ctx.write_json(summary_file, summary)

	lines = [
		f"run id: {ctx.paths.run_id}",
		f"output dir: {ctx.paths.root}",
		f"backend: {summary['backend_mode']}",
		f"records: {summary['record_count']}",
		f"failed records: {summary['failed_record_count']}",
		f"connected failures: {summary['connected_failure_count']}",
		f"non-connected failures: {summary['non_connected_failure_count']}",
	]
	for name, path in summary["files"].items():
		lines.append(f"{name}: {path}")
	return ctx.finish(summary, ok=summary["ok"], text_lines=lines)


if __name__ == "__main__":
	raise SystemExit(main())
