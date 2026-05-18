#!/usr/bin/env python3
"""Measure hardware timing overhead using Qiskit-authored circuits."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from qhw_util.output import backend_result_qhw
from qhw_util.output import to_jsonable
from qhw_util.schema import qhw_device_qubits
from qhw_util.workflow import WorkflowContext


def dry_run_result(cid: str, shots: int, num_circuits: int) -> dict[str, Any]:
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
					"num_circuits": num_circuits,
					"counts": {},
					"success": True,
				},
				"timing": {"timestamps": {}, "timeline": [],
					   "durations_seconds": {}},
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


def build_measure_circuit(width: int, name: str):
	try:
		from qiskit import QuantumCircuit
	except Exception as exc:
		raise RuntimeError(
			"qiskit is required for timing_overhead.py") from exc
	circuit = QuantumCircuit(width, width, name=name)
	circuit.measure(range(width), range(width))
	return circuit


def resolve_widths(widths: str, active_qubits: list[Any],
		   dry_run: bool) -> list[int]:
	resolved = []
	for raw in widths.split(","):
		raw = raw.strip()
		if not raw:
			continue
		if raw == "all":
			if active_qubits:
				resolved.append(len(active_qubits))
			elif dry_run:
				resolved.append(20)
			else:
				raise ValueError(
					"width 'all' requires backend active-qubit metadata")
			continue
		width = int(raw)
		if width < 1:
			raise ValueError(f"width must be positive: {raw!r}")
		resolved.append(width)
	if not resolved:
		raise ValueError("at least one width must be requested")
	return resolved


def extract_metrics(script_wall_seconds: float,
		    result: dict[str, Any]) -> dict[str, float | None]:
	qhw_result = backend_result_qhw(result)
	if not qhw_result:
		raise ValueError("backend result did not include normalized qhw_result")
	timing = qhw_result.get("timing", {})
	durations = timing.get("durations_seconds", {})
	return {
		"script_wall_seconds": script_wall_seconds,
		"client_total_seconds": None,
		"server_total_seconds": durations.get("provider_total_seconds"),
		"execution_seconds": durations.get("execution_seconds"),
	}


def safe_float(value: Any) -> float | None:
	if value is None:
		return None
	try:
		return float(value)
	except (TypeError, ValueError):
		return None


def as_list(value: Any) -> list[Any]:
	if value is None:
		return []
	return value if isinstance(value, list) else [value]


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


def build_fits(records: list[dict[str, Any]]) -> dict[str, Any]:
	metrics = [
		"script_wall_seconds",
		"client_total_seconds",
		"server_total_seconds",
		"execution_seconds",
	]
	fits = {"shot_scaling": {}, "batch_scaling": {}}
	for metric in metrics:
		shot_points = []
		batch_points = []
		for record in records:
			if not record.get("ok"):
				continue
			value = safe_float(record.get("metrics", {}).get(metric))
			if value is None:
				continue
			if (record.get("experiment") == "shot_sweep"
					and record.get("batch_size") == 1):
				shot_points.append((float(record["shots"]), value))
			if record.get("experiment") == "batch_sweep":
				batch_points.append((float(record["batch_size"]), value))
		fits["shot_scaling"][metric] = linear_fit(shot_points)
		fits["batch_scaling"][metric] = linear_fit(batch_points)
	return fits


def result_job_ids(result: dict[str, Any]) -> list[str]:
	qhw_result = backend_result_qhw(result)
	if not qhw_result:
		raise ValueError("backend result did not include normalized qhw_result")
	job_id = (qhw_result.get("job", {}) or {}).get("id")
	return [str(job_id)] if job_id else []


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	text = "\n".join(
		json.dumps(to_jsonable(record), sort_keys=True)
		for record in records)
	if text:
		text += "\n"
	path.write_text(text)


def add_script_args(parser: argparse.ArgumentParser) -> None:
	parser.add_argument("--shots-sweep", type=parse_int_list,
			    default=parse_int_list("1,10,100,1000"))
	parser.add_argument("--batch-sweep", type=parse_int_list,
			    default=parse_int_list("1,2,4"))
	parser.add_argument("--batch-shots", type=int, default=100)
	parser.add_argument("--widths", default="1")
	parser.add_argument("--repetitions", type=int, default=1)


def main() -> int:
	ctx = WorkflowContext.from_cli(
		__file__,
		description=(
			"Measure hardware fixed overhead, shot scaling, and "
			"batch scaling."),
		add_args=add_script_args,
		calibration=True,
		execution=True,
		dry_run=True,
	)
	args = ctx.args
	if args.repetitions < 1:
		raise ValueError("--repetitions must be at least 1")
	if args.batch_shots < 1:
		raise ValueError("--batch-shots must be at least 1")

	backend_info = {} if args.dry_run else to_jsonable(
		ctx.backend.get_backend_info())
	device_info = {} if args.dry_run else to_jsonable(
		ctx.backend.get_device_info())
	active_qubits = qhw_device_qubits(device_info)
	widths = resolve_widths(args.widths, active_qubits, args.dry_run)

	backend_info_file = ctx.paths.root / "backend_info.json"
	device_info_file = ctx.paths.root / "device_info.json"
	records_file = ctx.paths.results / "timing_records.jsonl"
	summary_file = ctx.paths.results / "timing_summary.json"
	ctx.write_json(backend_info_file, backend_info)
	ctx.write_json(device_info_file, device_info)

	records = []
	for repetition in range(args.repetitions):
		for width in widths:
			for shots in args.shots_sweep:
				cid = f"shot_w{width}_s{shots}_r{repetition}"
				circuit = build_measure_circuit(width, cid)
				start = time.monotonic()
				try:
					if args.dry_run:
						qasm_files = ctx.write_qasm_artifacts(
							[circuit], cid)
						result = dry_run_result(cid, shots, 1)
						run = ctx.write_backend_result(
							cid, result, qasm_files)
					else:
						run = ctx.run_circuit(
							[circuit], name=cid, shots=shots)
						result = run.result
					ok = run.ok
					error = None
				except Exception as exc:
					result = {"rc": 1, "error": str(exc)}
					run = None
					ok = False
					error = str(exc)
				wall = time.monotonic() - start
				records.append({
					"experiment": "shot_sweep",
					"ok": ok,
					"error": error,
					"repetition": repetition,
					"width": width,
					"shots": shots,
					"batch_size": 1,
					"backend_mode": args.backend if args.dry_run
					else ctx.backend.name,
					"batch_semantics": "single-circuit",
					"qasm_files": as_list(
						run.files.get("qasm") if run else None),
					"result_file": run.files.get("result") if run else None,
					"raw_result_file": (
						run.files.get("raw_result") if run else None),
					"normalized_result_file": (
						run.files.get("normalized_result") if run else None),
					"job_ids": result_job_ids(result) if run else [],
					"metrics": extract_metrics(wall, result) if run else {},
				})

			for batch_size in args.batch_sweep:
				cid = (
					f"batch_w{width}_b{batch_size}_"
					f"s{args.batch_shots}_r{repetition}")
				circuits = []
				for index in range(batch_size):
					circuit = build_measure_circuit(
						width, f"{cid}_{index}")
					circuits.append(circuit)
				start = time.monotonic()
				try:
					if args.dry_run:
						qasm_files = ctx.write_qasm_artifacts(
							circuits, cid)
						result = dry_run_result(
							cid, args.batch_shots, len(circuits))
						run = ctx.write_backend_result(
							cid, result, qasm_files)
					else:
						run = ctx.run_circuit(
							circuits,
							name=cid,
							shots=args.batch_shots)
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
				records.append({
					"experiment": "batch_sweep",
					"ok": ok,
					"error": error,
					"repetition": repetition,
					"width": width,
					"shots": args.batch_shots,
					"batch_size": batch_size,
					"backend_mode": args.backend if args.dry_run
					else ctx.backend.name,
					"batch_semantics": (
						"single-qhw-result"
						if qhw_result else "qiskit-backend-run"),
					"qasm_files": as_list(
						run.files.get("qasm") if run else None),
					"result_file": run.files.get("result") if run else None,
					"raw_result_file": (
						run.files.get("raw_result") if run else None),
					"normalized_result_file": (
						run.files.get("normalized_result") if run else None),
					"job_ids": result_job_ids(result) if run else [],
					"metrics": extract_metrics(wall, result) if run else {},
				})

	write_jsonl(records_file, records)
	summary = {
		"ok": all(record["ok"] for record in records),
		"run_id": ctx.paths.run_id,
		"date_id": ctx.paths.date_id,
		"output_dir": str(ctx.paths.root),
		"backend_mode": ctx.backend_name,
		"dry_run": args.dry_run,
		"config": {
			"shots_sweep": args.shots_sweep,
			"batch_sweep": args.batch_sweep,
			"batch_shots": args.batch_shots,
			"widths": widths,
			"repetitions": args.repetitions,
			"calibration_set_id": args.calibration_set_id,
		},
		"record_count": len(records),
		"failed_record_count": sum(
			1 for record in records if not record["ok"]),
		"fits": build_fits(records),
		"files": {
			"backend_info": str(backend_info_file),
			"device_info": str(device_info_file),
			"timing_records": str(records_file),
			"timing_summary": str(summary_file),
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
