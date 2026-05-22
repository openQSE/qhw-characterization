#!/usr/bin/env python3
"""Measure single-qubit gate timing using Qiskit-authored circuits."""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from qhw_util.output import backend_result_qhw
from qhw_util.output import qhw_json_path
from qhw_util.output import to_jsonable
from qhw_util.schema import qhw_device_qubits
from qhw_util.timing_model import execution_per_shot
from qhw_util.timing_model import expected_model_summary
from qhw_util.timing_model import one_q_baseline_table
from qhw_util.timing_model import one_q_sequence_model
from qhw_util.timing_model import sequence_key
from qhw_util.workflow import WorkflowContext

SUPPORTED_GATES = ("x", "rx", "ry")
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


def parse_gate_list(value: str) -> list[str]:
	gates = []
	for raw in value.split(","):
		gate = raw.strip().lower()
		if not gate:
			continue
		if gate not in SUPPORTED_GATES:
			raise argparse.ArgumentTypeError(
				f"unsupported 1Q gate {gate!r}; supported gates are "
				f"{', '.join(SUPPORTED_GATES)}")
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


def resolve_qubits(value: str, active_qubits: list[Any],
		   dry_run: bool) -> list[str]:
	if value == "all":
		if active_qubits:
			return [str(qubit) for qubit in active_qubits]
		if dry_run:
			return [f"QB{index}" for index in range(1, 21)]
		raise ValueError("qubit list 'all' requires backend active-qubit data")

	qubits = [item.strip() for item in value.split(",") if item.strip()]
	if not qubits:
		raise ValueError("qubit list must not be empty")
	return qubits


def apply_1q_gate(circuit, gate: str, qubit: int, angle: float) -> None:
	if gate == "x":
		circuit.x(qubit)
	elif gate == "rx":
		circuit.rx(angle, qubit)
	elif gate == "ry":
		circuit.ry(angle, qubit)
	else:
		raise ValueError(f"unsupported gate {gate!r}")


def build_gate_circuit(gate_sequence: list[str], depth: int,
		       angle: float, name: str):
	try:
		from qiskit import QuantumCircuit
	except Exception as exc:
		raise RuntimeError(
			"qiskit is required for timing_1q.py") from exc

	circuit = QuantumCircuit(1, 1, name=name)
	for _ in range(depth):
		for gate in gate_sequence:
			apply_1q_gate(circuit, gate, 0, angle)
	circuit.measure(0, 0)
	return circuit


def dry_run_1q_execution_seconds(gate_sequence: list[str],
				 depth: int, shots: int) -> float:
	per_gate = {
		"x": 0.00055,
		"rx": 0.00070,
		"ry": 0.00075,
	}
	per_shot = depth * sum(per_gate[gate] for gate in gate_sequence)
	return shots * per_shot


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


def build_fits(records: list[dict[str, Any]]) -> dict[str, Any]:
	metrics = [
		"execution_per_shot_seconds",
		"server_total_per_shot_seconds",
		"execution_seconds",
		"server_total_seconds",
		"client_total_seconds",
		"script_wall_seconds",
	]
	fits: dict[str, Any] = {
		"by_gate_qubit": {},
		"by_gate": {},
	}

	for record in records:
		if not record.get("ok"):
			continue
		gate = record["gate"]
		qubit = record["physical_qubit"]
		gate_qubit_key = f"{gate}:{qubit}"
		fits["by_gate_qubit"].setdefault(gate_qubit_key, {})
		fits["by_gate"].setdefault(gate, {})

	for group_name, group_key_fn in (
			("by_gate_qubit",
			 lambda record: f"{record['gate']}:{record['physical_qubit']}"),
			("by_gate", lambda record: record["gate"])):
		for key in list(fits[group_name].keys()):
			group_records = [
				record for record in records
				if record.get("ok") and group_key_fn(record) == key
			]
			for metric in metrics:
				points = []
				for record in group_records:
					value = safe_float(record.get("metrics", {}).get(metric))
					if value is None:
						continue
					points.append((float(record["depth"]), value))
				fits[group_name][key][metric] = linear_fit(points)

	return fits


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


def classify_fit(fit: dict[str, Any] | None) -> dict[str, Any]:
	if not fit:
		return {
			"status": "insufficient_data",
			"conclusion": (
				"Fewer than two successful records contain hardware execution "
				"timing, so this group cannot answer the depth-scaling "
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
			"The hardware execution-time data does not show a clear "
			"depth-dependent increase for this group. That can happen if "
			"the timing telemetry is too coarse for this circuit family, "
			"if the compiler optimized the repeated gates, or if the "
			"tested depths are too small.")
	elif fit["slope_seconds_per_gate"] > 0 and residual_fraction <= 0.2:
		status = "approximately_linear_positive"
		conclusion = (
			"Hardware execution time per shot increases approximately linearly "
			"with repeated 1Q sequence depth for this group.")
	elif fit["slope_seconds_per_gate"] > 0:
		status = "positive_but_noisy"
		conclusion = (
			"Hardware execution time per shot increases with depth, but the "
			"linear fit residuals are large enough that repeated runs or "
			"deeper circuits are needed before using the slope as a stable "
			"sequence-duration estimate.")
	else:
		status = "not_linear_positive"
		conclusion = (
			"The fitted slope is not positive, so this run does not support "
			"a linear per-sequence timing model for this group.")

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
	classification = classify_fit(fit)
	return {
		"record_count": len(records),
		"valid_primary_metric_count": len(points),
		"depths": sorted({record["depth"] for record in records}),
		"fit": fit,
		"classification": classification,
	}


def group_records(records: list[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
	grouped: dict[str, list[dict[str, Any]]] = {}
	for record in records:
		if not record.get("ok"):
			continue
		if key == "gate":
			group = record["gate"]
		elif key == "physical_qubit":
			group = record["physical_qubit"]
		elif key == "gate_qubit":
			group = f"{record['gate']}:{record['physical_qubit']}"
		else:
			raise ValueError(f"unsupported group key {key!r}")
		grouped.setdefault(group, []).append(record)
	return grouped


def build_analysis(records: list[dict[str, Any]],
		   config: dict[str, Any],
		   plots: dict[str, Any]) -> dict[str, Any]:
	by_gate_qubit = {
		key: analyze_group(group)
		for key, group in sorted(group_records(records, "gate_qubit").items())
	}
	by_gate = {
		key: analyze_group(group)
		for key, group in sorted(group_records(records, "gate").items())
	}
	by_qubit = {
		key: analyze_group(group)
		for key, group in sorted(group_records(records, "physical_qubit").items())
	}
	valid_count = sum(
		1 for record in records
		if record.get("ok")
		and safe_float(record.get("metrics", {}).get(PRIMARY_METRIC)) is not None)
	status_counts: dict[str, int] = {}
	for item in by_gate_qubit.values():
		status = item["classification"]["status"]
		status_counts[status] = status_counts.get(status, 0) + 1

	if valid_count == 0:
		overall_status = "no_hardware_execution_timing"
		overall_conclusion = (
			"No successful record contained hardware execution timing. This run "
			"cannot answer whether execution time scales with repeated 1Q "
			"gate depth.")
	elif status_counts.get("approximately_linear_positive", 0):
		overall_status = "depth_scaling_observed"
		overall_conclusion = (
			"At least one gate/qubit group shows approximately linear "
			"increase in hardware execution time per shot as repeated 1Q gate "
			"depth increases.")
	else:
		overall_status = "depth_scaling_not_established"
		overall_conclusion = (
			"This run did not establish a clean positive linear "
			"depth-scaling trend in the hardware execution-time metric.")

	return {
		"schema": "qhw-1q-analysis-v1",
		"intent": (
			"Determine whether hardware execution time increases linearly as "
			"more repetitions of a fixed 1Q gate sequence are added to a "
			"one-qubit circuit."),
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
		"overall": {
			"status": overall_status,
			"conclusion": overall_conclusion,
			"gate_qubit_status_counts": status_counts,
		},
		"by_gate_qubit": by_gate_qubit,
		"by_gate": by_gate,
		"by_qubit": by_qubit,
		"expected_model_summary": expected_model_summary(records),
		"plots": plots,
		"caveats": [
			"The script uses a repeated gate sequence instead of a single "
			"repeated gate to reduce compiler cancellation opportunities.",
			"Client wall time and server total time include non-hardware "
			"overheads and are not used for the primary hardware timing "
			"conclusion.",
			"Small depth sweeps may be below the resolution needed to infer "
			"a stable per-gate timing slope.",
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

	for gate, group in sorted(group_records(valid, "gate").items()):
		plt.figure(figsize=(8, 5))
		for qubit, qubit_records in sorted(group_records(
				group, "physical_qubit").items()):
			points = sorted((record["depth"], y(record))
					for record in qubit_records)
			xs = [point[0] for point in points]
			ys = [point[1] for point in points]
			plt.plot(xs, ys, marker="o", label=qubit)
		plt.xlabel("Repeated gate depth")
		plt.ylabel("Hardware execution time per shot (s)")
		plt.title(f"1Q {gate} timing by physical qubit")
		plt.xscale("log", base=2)
		plt.grid(True, which="both", alpha=0.3)
		if len(set(record["physical_qubit"] for record in group)) <= 12:
			plt.legend(fontsize="small")
		write_plot(f"1q_{gate}_all_qubits_execution_per_shot.png")

	for qubit, group in sorted(group_records(valid, "physical_qubit").items()):
		plt.figure(figsize=(8, 5))
		for gate, gate_records in sorted(group_records(group, "gate").items()):
			points = sorted((record["depth"], y(record))
					for record in gate_records)
			xs = [point[0] for point in points]
			ys = [point[1] for point in points]
			plt.plot(xs, ys, marker="o", label=gate)
		plt.xlabel("Repeated gate depth")
		plt.ylabel("Hardware execution time per shot (s)")
		plt.title(f"1Q timing on {qubit}")
		plt.xscale("log", base=2)
		plt.grid(True, which="both", alpha=0.3)
		plt.legend(fontsize="small")
		write_plot(f"1q_{qubit}_all_gates_execution_per_shot.png")

	for gate, group in sorted(group_records(valid, "gate").items()):
		plt.figure(figsize=(8, 5))
		for qubit, qubit_records in sorted(group_records(
				group, "physical_qubit").items()):
			points = metric_points(qubit_records, PRIMARY_METRIC)
			fit = linear_fit(points)
			if not fit:
				continue
			residuals = [
				(depth, value - (
					fit["intercept_seconds"]
					+ fit["slope_seconds_per_gate"] * depth))
				for depth, value in points
			]
			plt.plot(
				[point[0] for point in residuals],
				[point[1] for point in residuals],
				marker="o",
				label=qubit)
		plt.axhline(0, color="black", linewidth=1)
		plt.xlabel("Repeated gate depth")
		plt.ylabel("Residual execution time per shot (s)")
		plt.title(f"1Q {gate} fit residuals")
		plt.xscale("log", base=2)
		plt.grid(True, which="both", alpha=0.3)
		if len(set(record["physical_qubit"] for record in group)) <= 12:
			plt.legend(fontsize="small")
		write_plot(f"1q_{gate}_fit_residuals.png")

	slope_rows = []
	gates = sorted({record["gate"] for record in valid})
	qubits = sorted({record["physical_qubit"] for record in valid})
	for gate in gates:
		row = []
		for qubit in qubits:
			group = [
				record for record in valid
				if record["gate"] == gate
				and record["physical_qubit"] == qubit
			]
			fit = linear_fit(metric_points(group, PRIMARY_METRIC))
			row.append(
				fit["slope_seconds_per_gate"] if fit is not None else math.nan)
		slope_rows.append(row)
	if slope_rows and qubits:
		plt.figure(figsize=(max(8, len(qubits) * 0.35), max(3, len(gates) * 0.8)))
		image = plt.imshow(slope_rows, aspect="auto")
		plt.colorbar(image, label="Slope (s/depth/shot)")
		plt.xticks(range(len(qubits)), qubits, rotation=90)
		plt.yticks(range(len(gates)), gates)
		plt.title("1Q fitted execution-time slope")
		write_plot("1q_gate_slope_heatmap.png")

	return {
		"status": "generated",
		"metric": PRIMARY_METRIC,
		"files": files,
	}


def render_analysis_markdown(analysis: dict[str, Any]) -> str:
	lines = [
		"# 1Q Timing Analysis",
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
		"",
		"## Gate/Qubit Fits",
		"",
		"| Gate/Qubit | Status | Points | Slope (s/depth/shot) | RMS residual (s) | Conclusion |",
		"| --- | --- | ---: | ---: | ---: | --- |",
	]
	for key, item in analysis["by_gate_qubit"].items():
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
	parser.add_argument("--qubits", default="all")
	parser.add_argument("--gates", type=parse_gate_list,
			    default=parse_gate_list("rx,ry"))
	parser.add_argument("--depths", type=parse_int_list,
			    default=parse_int_list("1,2,4,8,16,32,64,128"))
	parser.add_argument("--shots", type=int, default=100)
	parser.add_argument("--repetitions", type=int, default=1)
	parser.add_argument("--angle", type=parse_angle, default=math.pi)


def main() -> int:
	ctx = WorkflowContext.from_cli(
		__file__,
		description=(
			"Measure single-qubit gate timing with Qiskit-authored "
			"circuits."),
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

	backend_info_file = ctx.paths.root / "backend_info.json"
	device_info_file = qhw_json_path(ctx.paths.root, "device_info")
	baseline_records_file = ctx.paths.results / "baseline_records.jsonl"
	records_file = ctx.paths.results / "timing_records.jsonl"
	summary_file = ctx.paths.results / "timing_summary.json"
	analysis_file = ctx.paths.results / "analysis.json"
	analysis_md_file = ctx.paths.results / "analysis.md"
	plots_dir = ctx.paths.results / "plots"
	ctx.write_json(backend_info_file, backend_info)
	ctx.write_json(device_info_file, device_info)

	baseline_records = []
	for repetition in range(args.repetitions):
		for qubit in qubits:
			for gate in args.gates:
				cid = (
					f"baseline_1q_{qubit}_{gate}_"
					f"s{args.shots}_r{repetition}")
				circuit = build_gate_circuit([gate], 1, args.angle, cid)
				start = time.monotonic()
				try:
					if args.dry_run:
						qasm_files = ctx.write_qasm_artifacts(
							[circuit], cid)
						result = dry_run_result(
							cid,
							args.shots,
							dry_run_1q_execution_seconds(
								[gate], 1, args.shots))
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
				baseline_records.append({
					"experiment": "single_qubit_gate_baseline",
					"ok": ok,
					"error": error,
					"repetition": repetition,
					"physical_qubit": qubit,
					"logical_qubits": 1,
					"gate": gate,
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

	baselines = one_q_baseline_table(baseline_records)

	records = []
	gate_sequence = list(args.gates)
	gate_sequence_key = sequence_key(gate_sequence)
	for repetition in range(args.repetitions):
		for qubit in qubits:
			for depth in args.depths:
				cid = (
					f"1q_{qubit}_{gate_sequence_key}_d{depth}_"
					f"s{args.shots}_r{repetition}")
				circuit = build_gate_circuit(
					gate_sequence, depth, args.angle, cid)

				start = time.monotonic()
				try:
					if args.dry_run:
						qasm_files = ctx.write_qasm_artifacts(
							[circuit], cid)
						result = dry_run_result(
							cid,
							args.shots,
							dry_run_1q_execution_seconds(
								gate_sequence, depth, args.shots))
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
				metrics = extract_metrics(
					wall, result, args.shots) if run else {}

				records.append({
					"experiment": "single_qubit_gate_timing",
					"ok": ok,
					"error": error,
					"repetition": repetition,
					"physical_qubit": qubit,
					"logical_qubits": 1,
					"gate": gate_sequence_key,
					"gate_sequence": gate_sequence,
					"sequence_repetitions": depth,
					"sequence_gate_count": depth * len(gate_sequence),
					"angle_radians": args.angle,
					"depth": depth,
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
					"metrics": metrics,
					"expected": one_q_sequence_model(
						baselines,
						qubit,
						gate_sequence,
						depth,
						execution_per_shot(metrics)),
				})

	write_jsonl(baseline_records_file, baseline_records)
	write_jsonl(records_file, records)
	config = {
		"qubits": qubits,
		"gate_sequence": gate_sequence,
		"gate_model": "Repeated Qiskit 1Q x/rx/ry gate sequence",
		"depths": args.depths,
		"shots": args.shots,
		"repetitions": args.repetitions,
		"angle_radians": args.angle,
		"calibration_set_id": args.calibration_set_id,
	}
	fits = build_fits(records)
	plots = plot_records(records, plots_dir)
	analysis = build_analysis(records, config, plots)
	ctx.write_json(analysis_file, analysis)
	analysis_md_file.write_text(render_analysis_markdown(analysis))

	summary = {
		"ok": all(record["ok"] for record in records),
		"run_id": ctx.paths.run_id,
		"date_id": ctx.paths.date_id,
		"output_dir": str(ctx.paths.root),
		"backend_mode": ctx.backend_name,
		"dry_run": args.dry_run,
		"config": config,
		"record_count": len(records),
		"failed_record_count": sum(
			1 for record in records if not record["ok"]),
		"fits": fits,
		"analysis": {
			"status": analysis["overall"]["status"],
			"conclusion": analysis["overall"]["conclusion"],
			"primary_metric": analysis["primary_metric"],
			"plots": plots,
		},
		"files": {
			"backend_info": str(backend_info_file),
			"device_info": str(device_info_file),
			"baseline_records": str(baseline_records_file),
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
	]
	for name, path in summary["files"].items():
		lines.append(f"{name}: {path}")
	return ctx.finish(summary, ok=summary["ok"], text_lines=lines)


if __name__ == "__main__":
	raise SystemExit(main())
