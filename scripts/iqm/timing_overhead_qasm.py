#!/usr/bin/env python3
"""Measure IQM fixed overhead, shot scaling, and batch scaling."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qhw_util.args import add_common_arguments
from qhw_util.backend import get_backend_from_args
from qhw_util.output import create_run_paths
from qhw_util.output import render_json_output
from qhw_util.output import render_text_output
from qhw_util.output import script_output_path
from qhw_util.output import to_jsonable
from qhw_util.output import write_json
from qhw_util.output import write_script_output


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


def build_measure_qasm(width: int) -> str:
	measures = "\n".join(
		f"measure q[{index}] -> c[{index}];" for index in range(width))
	return (
		"OPENQASM 2.0;\n"
		"include \"qelib1.inc\";\n"
		f"qreg q[{width}];\n"
		f"creg c[{width}];\n"
		f"{measures}\n"
	)


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


def build_info(qasm: str, args: argparse.Namespace,
	       shots: int, width: int, cid: str) -> dict[str, Any]:
	info = {
		"cid": cid,
		"qasm": qasm,
		"num_qubits": width,
		"num_shots": shots,
		"compiler": "iqm-native",
		"timeout": args.timeout,
		"use_timeslot": args.use_timeslot,
	}
	if args.calibration_set_id:
		info["calibration_set_id"] = args.calibration_set_id
	if args.qubit:
		if width != 1:
			raise ValueError("--qubit can only be used with width 1")
		info["iqm_qubit_mapping"] = {0: args.qubit}
	return info


def extract_iqm_payload(result: dict[str, Any]) -> dict[str, Any]:
	payload = result.get("result", {})
	if not isinstance(payload, dict):
		return {}
	iqm_payload = payload.get("iqm", {})
	return iqm_payload if isinstance(iqm_payload, dict) else {}


def extract_metrics(script_wall_seconds: float,
		    result: dict[str, Any]) -> dict[str, float | None]:
	iqm_payload = extract_iqm_payload(result)
	timing_summary = iqm_payload.get("timing_summary") or {}
	client_wall = timing_summary.get("client_wall_seconds", {})
	durations = timing_summary.get("durations_seconds", {})
	return {
		"script_wall_seconds": script_wall_seconds,
		"client_total_seconds": client_wall.get("total"),
		"server_total_seconds": durations.get(
			"server_total_created_to_completed"),
		"execution_seconds": durations.get("execution"),
	}


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
	iqm_payload = extract_iqm_payload(result)
	job_id = iqm_payload.get("job_id")
	if job_id:
		return [str(job_id)]
	results = result.get("result", {}).get("results", [])
	job_ids = []
	for item in results if isinstance(results, list) else []:
		item_iqm = extract_iqm_payload(item)
		if item_iqm.get("job_id"):
			job_ids.append(str(item_iqm["job_id"]))
	return job_ids


def run_case(backend, infos: list[dict[str, Any]], dry_run: bool):
	if dry_run:
		return {
			"cid": infos[0]["cid"],
			"result": {
				"dry_run": True,
				"batch_semantics": "not-submitted",
			},
			"rc": 0,
		}
	if len(infos) == 1:
		return to_jsonable(backend.sync_run(infos[0]))
	return to_jsonable(backend.sync_run_many(infos))


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	text = "\n".join(
		json.dumps(to_jsonable(record), sort_keys=True)
		for record in records)
	if text:
		text += "\n"
	path.write_text(text)


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(
		description=(
			"Measure IQM fixed overhead, shot scaling, and batch scaling."),
	)
	parser.add_argument("--shots-sweep", type=parse_int_list,
			    default=parse_int_list("1,10,100,1000"))
	parser.add_argument("--batch-sweep", type=parse_int_list,
			    default=parse_int_list("1,2,4"))
	parser.add_argument("--batch-shots", type=int, default=100)
	parser.add_argument("--widths", default="1")
	parser.add_argument("--repetitions", type=int, default=1)
	parser.add_argument("--qubit", default=None)
	add_common_arguments(
		parser, calibration=True, execution=True, dry_run=True)
	return parser.parse_args()


def main() -> int:
	args = parse_args()
	if args.repetitions < 1:
		raise ValueError("--repetitions must be at least 1")
	if args.batch_shots < 1:
		raise ValueError("--batch-shots must be at least 1")

	paths = create_run_paths(__file__, args.output_dir, args.run_id)
	backend = None if args.dry_run else get_backend_from_args(args)
	backend_info = {} if args.dry_run else to_jsonable(
		backend.get_backend_info())
	active_qubits = backend_info.get("active_qubits", [])
	widths = resolve_widths(args.widths, active_qubits, args.dry_run)

	backend_info_file = paths.root / "backend_info.json"
	records_file = paths.results / "timing_records.jsonl"
	summary_file = paths.results / "timing_summary.json"
	write_json(backend_info_file, backend_info)

	records = []
	for repetition in range(args.repetitions):
		for width in widths:
			for shots in args.shots_sweep:
				cid = (
					f"shot_w{width}_s{shots}_r{repetition}")
				qasm = build_measure_qasm(width)
				qasm_file = paths.circuits / f"{cid}.qasm"
				result_file = paths.results / f"{cid}.json"
				qasm_file.write_text(qasm)
				info = build_info(qasm, args, shots, width, cid)
				start = time.monotonic()
				try:
					result = run_case(backend, [info], args.dry_run)
					ok = result.get("rc") == 0
					error = None
				except Exception as exc:
					result = {"rc": 1, "error": str(exc)}
					ok = False
					error = str(exc)
				wall = time.monotonic() - start
				write_json(result_file, result)
				records.append({
					"experiment": "shot_sweep",
					"ok": ok,
					"error": error,
					"repetition": repetition,
					"width": width,
					"shots": shots,
					"batch_size": 1,
					"backend_mode": args.backend if args.dry_run
					else backend.name,
					"batch_semantics": "single-circuit",
					"qasm_files": [str(qasm_file)],
					"result_file": str(result_file),
					"job_ids": result_job_ids(result),
					"metrics": extract_metrics(wall, result),
				})

			for batch_size in args.batch_sweep:
				cid = (
					f"batch_w{width}_b{batch_size}_"
					f"s{args.batch_shots}_r{repetition}")
				infos = []
				qasm_files = []
				for index in range(batch_size):
					qasm = build_measure_qasm(width)
					qasm_file = paths.circuits / f"{cid}_{index}.qasm"
					qasm_file.write_text(qasm)
					qasm_files.append(str(qasm_file))
					infos.append(build_info(
						qasm, args, args.batch_shots, width,
						f"{cid}_{index}"))
				result_file = paths.results / f"{cid}.json"
				start = time.monotonic()
				try:
					result = run_case(backend, infos, args.dry_run)
					ok = result.get("rc") == 0
					error = None
				except Exception as exc:
					result = {"rc": 1, "error": str(exc)}
					ok = False
					error = str(exc)
				wall = time.monotonic() - start
				write_json(result_file, result)
				iqm_payload = extract_iqm_payload(result)
				records.append({
					"experiment": "batch_sweep",
					"ok": ok,
					"error": error,
					"repetition": repetition,
					"width": width,
					"shots": args.batch_shots,
					"batch_size": batch_size,
					"backend_mode": args.backend if args.dry_run
					else backend.name,
					"batch_semantics": iqm_payload.get(
						"batch_semantics",
						result.get("result", {}).get(
							"batch_semantics", "unknown")),
					"qasm_files": qasm_files,
					"result_file": str(result_file),
					"job_ids": result_job_ids(result),
					"metrics": extract_metrics(wall, result),
				})

	write_jsonl(records_file, records)
	summary = {
		"ok": all(record["ok"] for record in records),
		"run_id": paths.run_id,
		"date_id": paths.date_id,
		"output_dir": str(paths.root),
		"backend_mode": args.backend if args.dry_run else backend.name,
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
			"timing_records": str(records_file),
			"timing_summary": str(summary_file),
		},
	}
	summary["files"]["script_output"] = str(
		script_output_path(paths, args.json))
	write_json(summary_file, summary)

	if args.json:
		output = render_json_output(summary)
	else:
		lines = [
			f"run id: {summary['run_id']}",
			f"output dir: {summary['output_dir']}",
			f"backend: {summary['backend_mode']}",
			f"records: {summary['record_count']}",
			f"failed records: {summary['failed_record_count']}",
		]
		for name, path in summary["files"].items():
			lines.append(f"{name}: {path}")
		output = render_text_output(lines)
	write_script_output(paths, output, args.json)

	rc = 0 if summary["ok"] else 2
	return rc if args.dry_run else backend.finish(rc)


if __name__ == "__main__":
	raise SystemExit(main())
