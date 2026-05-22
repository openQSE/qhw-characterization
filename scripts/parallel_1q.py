#!/usr/bin/env python3
"""Measure whether one-qubit gates execute in parallel across qubits."""

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
from qhw_util.timing_model import parallel_one_q_sequence_model
from qhw_util.timing_model import sequence_key
from qhw_util.workflow import WorkflowContext

SUPPORTED_GATES = ("x", "rx", "ry")
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
		if gate not in SUPPORTED_GATES:
			raise argparse.ArgumentTypeError(
				f"unsupported 1Q gate {gate!r}; expected one of "
				f"{', '.join(SUPPORTED_GATES)}")
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


def active_qubits_for_dry_run() -> list[str]:
	return [f"QB{index}" for index in range(1, 21)]


def resolve_qubits(value: str, active_qubits: list[str],
		   dry_run: bool) -> list[str]:
	if value.strip().lower() == "all":
		if active_qubits:
			return active_qubits
		if dry_run:
			return active_qubits_for_dry_run()
		raise ValueError("qubits=all requires backend device metadata")
	qubits = [item.strip() for item in value.split(",") if item.strip()]
	if not qubits:
		raise ValueError("at least one qubit must be selected")
	return qubits


def resolve_widths(value: str, qubits: list[str]) -> list[int]:
	max_width = len(qubits)
	widths = []
	for raw in value.split(","):
		raw = raw.strip().lower()
		if not raw:
			continue
		if raw == "all":
			widths.extend(range(1, max_width + 1))
			continue
		if raw == "max":
			widths.append(max_width)
			continue
		width = int(raw)
		if width < 1:
			raise ValueError(f"width must be positive: {raw!r}")
		if width > max_width:
			raise ValueError(
				f"width {width} exceeds selected qubit count {max_width}")
		widths.append(width)
	if not widths:
		raise ValueError("at least one width must be selected")
	return sorted(set(widths))


def apply_1q_gate(circuit, gate: str, qubit: int, angle: float) -> None:
	if gate == "x":
		circuit.x(qubit)
	elif gate == "rx":
		circuit.rx(angle, qubit)
	elif gate == "ry":
		circuit.ry(angle, qubit)
	else:
		raise ValueError(f"unsupported gate: {gate}")


def build_parallel_1q_circuit(gate_sequence: list[str], width: int,
			      depth: int, angle: float, name: str):
	try:
		from qiskit import QuantumCircuit
	except Exception as exc:
		raise RuntimeError(
			"qiskit is required for parallel_1q.py") from exc
	circuit = QuantumCircuit(width, width, name=name)
	for _ in range(depth):
		for gate in gate_sequence:
			for qubit in range(width):
				apply_1q_gate(circuit, gate, qubit, angle)
		if width > 1:
			circuit.barrier()
	circuit.measure(range(width), range(width))
	return circuit


def dry_run_result(cid: str, shots: int, depth: int, width: int,
		   gate_sequence: list[str]) -> dict[str, Any]:
	per_gate = {
		"x": 0.00055,
		"rx": 0.00070,
		"ry": 0.00075,
	}
	parallel_layer_seconds = sum(per_gate[gate] for gate in gate_sequence)
	congestion_seconds = 0.00001 * max(0, width - 1)
	execution_seconds = shots * depth * (
		parallel_layer_seconds + congestion_seconds)
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


def classify_width_fit(fit: dict[str, Any] | None) -> dict[str, Any]:
	if not fit:
		return {
			"status": "insufficient_data",
			"conclusion": "Not enough width points to assess parallelism.",
		}
	span = fit["x_max"] - fit["x_min"]
	predicted_delta = fit["slope_seconds_per_unit"] * span
	mean_value = abs(fit["y_mean"]) if fit["y_mean"] else 0.0
	delta_fraction = abs(predicted_delta) / mean_value if mean_value else None
	if delta_fraction is not None and delta_fraction < 0.10:
		status = "width_independent"
		conclusion = (
			"Execution time per shot is approximately independent of active "
			"1Q layer width for this depth/gate group.")
	elif fit["slope_seconds_per_unit"] > 0:
		status = "width_dependent"
		conclusion = (
			"Execution time per shot increases with active 1Q layer width.")
	else:
		status = "not_width_increasing"
		conclusion = (
			"Execution time per shot did not increase with active 1Q width.")
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
			fit = linear_fit(metric_points(group, "width", PRIMARY_METRIC))
			by_gate_depth[key] = {
				"record_count": len(group),
				"widths": sorted({record["width"] for record in group}),
				"fit": fit,
				"classification": classify_width_fit(fit),
			}
	status_counts: dict[str, int] = {}
	for item in by_gate_depth.values():
		status = item["classification"]["status"]
		status_counts[status] = status_counts.get(status, 0) + 1
	if not successful:
		status = "no_hardware_execution_timing"
		conclusion = "No successful record contained hardware execution timing."
	elif status_counts.get("width_dependent", 0):
		status = "width_dependence_observed"
		conclusion = (
			"At least one 1Q layer group shows width-dependent execution "
			"time, which argues against fully parallel layer execution.")
	else:
		status = "width_dependence_not_established"
		conclusion = (
			"This run did not establish a strong active-width timing term.")
	return {
		"schema": "qhw-parallel-1q-analysis-v1",
		"intent": (
			"Determine whether simultaneous one-qubit gate-sequence layers "
			"execute in parallel by sweeping active qubit count at fixed "
			"depth."),
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
			"The script uses a repeated gate sequence instead of a single "
			"repeated gate to reduce compiler cancellation opportunities.",
			"Width-independence in timing does not imply equal fidelity.",
			"Client and server total times include non-hardware overheads.",
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
		for width, group in sorted(
				group_records(gate_records, "width").items(),
				key=lambda item: int(item[0])):
			points = sorted(
				(record["depth"], metric(record))
				for record in group if metric(record) is not None)
			plt.plot(
				[point[0] for point in points],
				[point[1] for point in points],
				marker="o",
				label=f"width {width}")
		plt.xlabel("Repeated 1Q layer depth")
		plt.ylabel("Hardware execution time per shot (s)")
		plt.title(f"Parallel 1Q {gate} timing by active width")
		plt.xscale("log", base=2)
		plt.grid(True, which="both", alpha=0.3)
		plt.legend(fontsize="small")
		write_plot(f"parallel_1q_{gate}_depth_by_width.png")

	for gate, gate_records in sorted(group_records(valid, "gate").items()):
		plt.figure(figsize=(9, 5))
		for depth, group in sorted(
				group_records(gate_records, "depth").items(),
				key=lambda item: int(item[0])):
			points = sorted(
				(record["width"], metric(record))
				for record in group if metric(record) is not None)
			plt.plot(
				[point[0] for point in points],
				[point[1] for point in points],
				marker="o",
				label=f"depth {depth}")
		plt.xlabel("Active qubits in simultaneous 1Q layer")
		plt.ylabel("Hardware execution time per shot (s)")
		plt.title(f"Parallel 1Q {gate} width scaling")
		plt.grid(True, alpha=0.3)
		plt.legend(fontsize="small")
		write_plot(f"parallel_1q_{gate}_width_scaling.png")

	return {
		"status": "generated",
		"metric": PRIMARY_METRIC,
		"files": files,
	}


def render_analysis_markdown(analysis: dict[str, Any]) -> str:
	lines = [
		"# Parallel 1Q Timing Analysis",
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
		"## Gate/Depth Width Fits",
		"",
		"| Gate/Depth | Status | Widths | Slope (s/qubit/shot) | Conclusion |",
		"| --- | --- | --- | ---: | --- |",
	]
	for key, item in analysis["by_gate_depth"].items():
		fit = item.get("fit") or {}
		classification = item["classification"]
		lines.append(
			f"| `{key}` | `{classification['status']}` | "
			f"{item['widths']} | {fit.get('slope_seconds_per_unit', '')} | "
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
	parser.add_argument("--qubits", default="all")
	parser.add_argument("--widths", default="1,2,4,8,max")
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
			"Measure simultaneous one-qubit gate layer timing."),
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
					f"baseline_parallel_1q_{qubit}_{gate}_"
					f"s{args.shots}_r{repetition}")
				circuit = build_parallel_1q_circuit(
					[gate], 1, 1, args.angle, cid)
				start = time.monotonic()
				try:
					if args.dry_run:
						qasm_files = ctx.write_qasm_artifacts(
							circuit, cid)
						result = dry_run_result(
							cid, args.shots, 1, 1, [gate])
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
						"experiment": "parallel_1q_single_gate_baseline",
						"submission_path": "backend.run",
						"gate": gate,
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
						"experiment": "parallel_1q_single_gate_baseline",
						"gate": gate,
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
				baseline_records.append(record)

	baselines = one_q_baseline_table(baseline_records)

	records = []
	gate_sequence = list(args.gates)
	gate_sequence_key = sequence_key(gate_sequence)
	for repetition in range(args.repetitions):
		for width in widths:
			physical_qubits = qubits[:width]
			mapping = {
				index: qubit
				for index, qubit in enumerate(physical_qubits)
			}
			for depth in args.depths:
				cid = (
					f"parallel_1q_{gate_sequence_key}_w{width}_d{depth}_"
					f"s{args.shots}_r{repetition}")
				circuit = build_parallel_1q_circuit(
					gate_sequence, width, depth, args.angle, cid)
				start = time.monotonic()
				try:
					if args.dry_run:
						qasm_files = ctx.write_qasm_artifacts(
							circuit, cid)
						result = dry_run_result(
							cid, args.shots, depth, width, gate_sequence)
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
						"gate": gate_sequence_key,
						"gate_sequence": gate_sequence,
						"sequence_repetitions": depth,
						"sequence_gate_count_per_qubit": (
							depth * len(gate_sequence)),
						"width": width,
						"depth": depth,
						"shots": args.shots,
						"repetition": repetition,
						"physical_qubits": physical_qubits,
						"qubit_mapping": {
							str(key): value for key, value in mapping.items()
						},
						"job_id": run.job_id,
						"metrics": metrics,
						"expected": parallel_one_q_sequence_model(
							baselines,
							physical_qubits,
							gate_sequence,
							depth,
							execution_per_shot(metrics)),
						"counts": run.counts,
						"files": run.files,
					}
				except Exception as exc:
					record = {
						"ok": False,
						"circuit_id": cid,
						"gate": gate_sequence_key,
						"gate_sequence": gate_sequence,
						"width": width,
						"depth": depth,
						"shots": args.shots,
						"repetition": repetition,
						"physical_qubits": physical_qubits,
						"qubit_mapping": {
							str(key): value for key, value in mapping.items()
						},
						"error": str(exc),
						"metrics": {},
						"files": {},
					}
				records.append(record)

	write_jsonl(baseline_records_file, baseline_records)
	write_jsonl(records_file, records)
	config = {
		"backend": ctx.backend_name,
		"qubits": qubits,
		"widths": widths,
		"gate_sequence": gate_sequence,
		"depths": args.depths,
		"shots": args.shots,
		"repetitions": args.repetitions,
		"angle": args.angle,
		"dry_run": args.dry_run,
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
			"device_info": str(device_info_file),
			"baseline_records": str(baseline_records_file),
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
