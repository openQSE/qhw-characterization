#!/usr/bin/env python3
"""Estimate coherence behavior with delay-based Qiskit circuits."""

from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from qhw_util.experiments import dry_run_result
from qhw_util.experiments import exponential_decay_fit
from qhw_util.experiments import logical_one_probability
from qhw_util.experiments import mean_or_none
from qhw_util.experiments import parse_float_list
from qhw_util.experiments import resolve_qubits
from qhw_util.experiments import write_jsonl
from qhw_util.output import qhw_json_path
from qhw_util.output import to_jsonable
from qhw_util.schema import qhw_device_qubits
from qhw_util.workflow import WorkflowContext


SUPPORTED_EXPERIMENTS = ("t1", "ramsey", "echo")


def parse_experiments(value: str) -> list[str]:
	experiments = []
	for raw in value.split(","):
		item = raw.strip().lower()
		if not item:
			continue
		if item not in SUPPORTED_EXPERIMENTS:
			raise argparse.ArgumentTypeError(
				f"unsupported experiment {item!r}; expected one of "
				f"{', '.join(SUPPORTED_EXPERIMENTS)}")
		experiments.append(item)
	if not experiments:
		raise argparse.ArgumentTypeError("experiment list must not be empty")
	return experiments


def probability_counts(p_one: float, shots: int) -> dict[str, int]:
	p_one = min(max(p_one, 0.0), 1.0)
	ones = int(round(p_one * shots))
	zeros = shots - ones
	counts = {}
	if zeros:
		counts["0"] = zeros
	if ones:
		counts["1"] = ones
	return counts


def dry_run_probability(experiment: str, delay_us: float,
			t1_us: float, t2_us: float) -> float:
	if experiment == "t1":
		return math.exp(-delay_us / t1_us)
	if experiment == "ramsey":
		fringe = math.cos(delay_us * 0.35)
		return 0.5 - 0.45 * fringe * math.exp(-delay_us / t2_us)
	if experiment == "echo":
		return 0.5 - 0.45 * math.exp(-delay_us / (t2_us * 1.8))
	raise ValueError(f"unsupported experiment {experiment!r}")


def build_coherence_circuit(experiment: str, delay_us: float, name: str):
	try:
		from qiskit import QuantumCircuit
	except Exception as exc:
		raise RuntimeError("qiskit is required for coherence.py") from exc

	circuit = QuantumCircuit(1, 1, name=name)
	if experiment == "t1":
		circuit.x(0)
		circuit.delay(delay_us, 0, unit="us")
	elif experiment == "ramsey":
		circuit.rx(math.pi / 2, 0)
		circuit.delay(delay_us, 0, unit="us")
		circuit.rx(math.pi / 2, 0)
	elif experiment == "echo":
		circuit.rx(math.pi / 2, 0)
		circuit.delay(delay_us / 2, 0, unit="us")
		circuit.x(0)
		circuit.delay(delay_us / 2, 0, unit="us")
		circuit.rx(math.pi / 2, 0)
	else:
		raise ValueError(f"unsupported experiment {experiment!r}")
	circuit.measure(0, 0)
	return circuit


def run_record(ctx: WorkflowContext, *, experiment: str, qubit: str,
	       delay_us: float, shots: int, repetition: int,
	       dry_run_t1_us: float, dry_run_t2_us: float) -> dict[str, Any]:
	cid = (
		f"coherence_{experiment}_{qubit}_d{delay_us:g}_"
		f"s{shots}_r{repetition}")
	circuit = build_coherence_circuit(experiment, delay_us, cid)
	mapping = {0: qubit}
	start = time.monotonic()
	try:
		if ctx.args.dry_run:
			qasm_files = ctx.write_qasm_artifacts(circuit, cid)
			p_one = dry_run_probability(
				experiment, delay_us, dry_run_t1_us, dry_run_t2_us)
			result = dry_run_result(
				cid,
				shots,
				probability_counts(p_one, shots),
				execution_seconds=shots * (0.0003 + delay_us * 1e-6))
			run = ctx.write_backend_result(cid, result, qasm_files)
		else:
			run = ctx.run_circuit(
				circuit,
				name=cid,
				qasm_name=cid,
				shots=shots,
				qubit_mapping=mapping)
		script_wall = time.monotonic() - start
		p_one = logical_one_probability(run.counts or {}, 1, 0)
		return {
			"ok": run.ok,
			"experiment": experiment,
			"physical_qubit": qubit,
			"delay_us": delay_us,
			"shots": shots,
			"repetition": repetition,
			"job_id": run.job_id,
			"counts": run.counts or {},
			"metrics": {
				"script_wall_seconds": script_wall,
				"p_one": p_one,
				"p_zero": 1.0 - p_one if p_one is not None else None,
			},
			"files": run.files,
		}
	except Exception as exc:
		return {
			"ok": False,
			"experiment": experiment,
			"physical_qubit": qubit,
			"delay_us": delay_us,
			"shots": shots,
			"repetition": repetition,
			"error": str(exc),
			"counts": {},
			"metrics": {},
			"files": {},
		}


def fit_series(records: list[dict[str, Any]]) -> dict[str, Any]:
	grouped: dict[str, list[dict[str, Any]]] = {}
	for record in records:
		if not record.get("ok"):
			continue
		key = f"{record['experiment']}:{record['physical_qubit']}"
		grouped.setdefault(key, []).append(record)

	fits = {}
	for key, items in grouped.items():
		experiment, qubit = key.split(":", 1)
		by_delay: dict[float, list[float]] = {}
		for item in items:
			p_one = item.get("metrics", {}).get("p_one")
			if p_one is None:
				continue
			if experiment == "t1":
				value = p_one
			else:
				value = abs((1.0 - p_one) - 0.5)
			by_delay.setdefault(float(item["delay_us"]), []).append(value)
		points = [
			(delay, mean_or_none(values))
			for delay, values in sorted(by_delay.items())
		]
		points = [(delay, value) for delay, value in points if value is not None]
		fits[key] = {
			"experiment": experiment,
			"physical_qubit": qubit,
			"points": [
				{"delay_us": delay, "value": value}
				for delay, value in points
			],
			"decay_fit": exponential_decay_fit(points, floor=0.0),
		}
	return fits


def build_analysis(records: list[dict[str, Any]],
		   config: dict[str, Any]) -> dict[str, Any]:
	fits = fit_series(records)
	return {
		"schema": "qhw-coherence-analysis-v1",
		"intent": (
			"Estimate T1-like and T2-like decay trends using delay sweeps."),
		"config": config,
		"record_count": len(records),
		"successful_record_count": sum(
			1 for record in records if record.get("ok")),
		"failed_record_count": sum(
			1 for record in records if not record.get("ok")),
		"fits": fits,
		"caveats": [
			"These circuits estimate decay trends from job results; exact "
			"pulse-level T1/T2 calibration remains provider-specific.",
			"Ramsey and echo fits use contrast around 0.5 rather than a full "
			"oscillation model.",
		],
	}


def render_analysis_markdown(analysis: dict[str, Any]) -> str:
	lines = [
		"# Coherence Analysis",
		"",
		analysis["intent"],
		"",
		"## Summary",
		"",
		f"Records: {analysis['record_count']}",
		f"Successful records: {analysis['successful_record_count']}",
		f"Failed records: {analysis['failed_record_count']}",
		"",
		"## Fits",
		"",
		"| Experiment | Qubit | Estimated decay constant (us) | Points |",
		"| --- | --- | ---: | ---: |",
	]
	for item in analysis["fits"].values():
		fit = item.get("decay_fit") or {}
		lines.append(
			f"| `{item['experiment']}` | `{item['physical_qubit']}` | "
			f"{fit.get('decay_constant')} | {fit.get('points', 0)} |")
	lines += ["", "## Caveats", ""]
	for caveat in analysis["caveats"]:
		lines.append(f"- {caveat}")
	return "\n".join(lines) + "\n"


def add_script_args(parser: argparse.ArgumentParser) -> None:
	parser.add_argument("--qubits", default="all")
	parser.add_argument("--experiments", type=parse_experiments,
			    default=parse_experiments("t1,ramsey,echo"))
	parser.add_argument("--delays-us", type=parse_float_list,
			    default=parse_float_list("1,2,4,8,16,32,64"))
	parser.add_argument("--shots", type=int, default=1000)
	parser.add_argument("--repetitions", type=int, default=1)
	parser.add_argument("--dry-run-t1-us", type=float, default=45.0)
	parser.add_argument("--dry-run-t2-us", type=float, default=18.0)


def main() -> int:
	ctx = WorkflowContext.from_cli(
		__file__,
		description="Run delay-based coherence characterization circuits.",
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
	qubits = resolve_qubits(
		args.qubits, qhw_device_qubits(device_info), args.dry_run)

	backend_info_file = ctx.paths.root / "backend_info.json"
	device_info_file = qhw_json_path(ctx.paths.root, "device_info")
	records_file = ctx.paths.results / "coherence_records.jsonl"
	analysis_file = ctx.paths.results / "analysis.json"
	analysis_md_file = ctx.paths.results / "analysis.md"
	summary_file = ctx.paths.results / "coherence_summary.json"
	ctx.write_json(backend_info_file, backend_info)
	ctx.write_json(device_info_file, device_info)

	records = []
	for repetition in range(args.repetitions):
		for experiment in args.experiments:
			for qubit in qubits:
				for delay_us in args.delays_us:
					records.append(run_record(
						ctx,
						experiment=experiment,
						qubit=qubit,
						delay_us=float(delay_us),
						shots=args.shots,
						repetition=repetition,
						dry_run_t1_us=args.dry_run_t1_us,
						dry_run_t2_us=args.dry_run_t2_us))

	write_jsonl(records_file, records)
	config = {
		"backend": ctx.backend_name,
		"qubits": qubits,
		"experiments": args.experiments,
		"delays_us": args.delays_us,
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
