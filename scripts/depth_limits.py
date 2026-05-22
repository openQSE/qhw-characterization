#!/usr/bin/env python3
"""Sweep circuit width/depth to estimate useful-depth limits."""

from __future__ import annotations

import argparse
import math
import random
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from qhw_util.experiments import dry_run_result
from qhw_util.experiments import normalize_count_key
from qhw_util.experiments import parse_int_list
from qhw_util.experiments import resolve_qubits
from qhw_util.experiments import resolve_widths
from qhw_util.experiments import write_jsonl
from qhw_util.output import qhw_json_path
from qhw_util.output import to_jsonable
from qhw_util.schema import qhw_device_qubits
from qhw_util.workflow import WorkflowContext


SUPPORTED_FAMILIES = ("ghz", "mirror", "hardware_efficient")


def parse_families(value: str) -> list[str]:
	families = []
	for raw in value.split(","):
		item = raw.strip().lower()
		if not item:
			continue
		if item not in SUPPORTED_FAMILIES:
			raise argparse.ArgumentTypeError(
				f"unsupported family {item!r}; expected one of "
				f"{', '.join(SUPPORTED_FAMILIES)}")
		families.append(item)
	if not families:
		raise argparse.ArgumentTypeError("family list must not be empty")
	return families


def build_family_circuit(family: str, width: int, depth: int, seed: int):
	try:
		from qiskit import QuantumCircuit
	except Exception as exc:
		raise RuntimeError("qiskit is required for depth_limits.py") from exc

	body = QuantumCircuit(width, name=f"{family}_body")
	if family == "ghz":
		body.h(0)
		for qubit in range(width - 1):
			body.cx(qubit, qubit + 1)
		for layer in range(max(depth - 1, 0)):
			for qubit in range(width):
				body.rx((layer + 1) * math.pi / 17, qubit)
			for qubit in range(width - 1):
				body.cz(qubit, qubit + 1)
	elif family == "mirror":
		rng = random.Random(seed)
		gates = []
		for _ in range(depth):
			for qubit in range(width):
				gate = rng.choice(("x", "h"))
				gates.append((gate, qubit))
				getattr(body, gate)(qubit)
			for qubit in range(width - 1):
				gates.append(("cx", qubit, qubit + 1))
				body.cx(qubit, qubit + 1)
		for gate in reversed(gates):
			if gate[0] == "cx":
				body.cx(gate[1], gate[2])
			else:
				getattr(body, gate[0])(gate[1])
	elif family == "hardware_efficient":
		for layer in range(depth):
			for qubit in range(width):
				body.ry((layer + qubit + 1) * math.pi / 13, qubit)
				body.rx((layer + qubit + 1) * math.pi / 19, qubit)
			for qubit in range(layer % 2, width - 1, 2):
				body.cx(qubit, qubit + 1)
	else:
		raise ValueError(f"unsupported family {family!r}")

	measured = QuantumCircuit(width, width, name=f"{family}_w{width}_d{depth}")
	measured.compose(body, inplace=True)
	measured.measure(range(width), range(width))
	return body, measured


def ideal_probabilities(circuit, width: int,
			max_ideal_width: int) -> dict[str, float] | None:
	if width > max_ideal_width:
		return None
	try:
		from qiskit.quantum_info import Statevector
	except Exception:
		return None
	probs = Statevector.from_instruction(circuit).probabilities_dict()
	return {
		normalize_count_key(key, width): float(value)
		for key, value in probs.items()
		if value > 1e-12
	}


def distribution_counts(probabilities: dict[str, float] | None, width: int,
			shots: int, error: float) -> dict[str, int]:
	if not probabilities:
		probabilities = {"0" * width: 1.0}
	uniform = 1.0 / (1 << width)
	mixed = {}
	for state in range(1 << width):
		key = format(state, f"0{width}b")
		ideal = probabilities.get(key, 0.0)
		mixed[key] = (1.0 - error) * ideal + error * uniform
	counts = {key: int(round(value * shots)) for key, value in mixed.items()}
	delta = shots - sum(counts.values())
	if delta:
		best_key = max(counts, key=counts.get)
		counts[best_key] += delta
	return {key: value for key, value in counts.items() if value}


def hellinger_fidelity(counts: dict[str, Any], probabilities: dict[str, float],
		       width: int) -> float | None:
	total = sum(int(value) for value in counts.values())
	if total <= 0 or not probabilities:
		return None
	keys = set(probabilities)
	keys.update(normalize_count_key(key, width) for key in counts)
	overlap = 0.0
	for key in keys:
		observed = 0.0
		for raw_key, value in counts.items():
			if normalize_count_key(raw_key, width) == key:
				observed += int(value) / total
		overlap += math.sqrt(observed * probabilities.get(key, 0.0))
	return overlap * overlap


def run_record(ctx: WorkflowContext, *, family: str, width: int,
	       depth: int, physical_qubits: list[str], shots: int,
	       repetition: int, seed: int, max_ideal_width: int,
	       dry_run_error: float) -> dict[str, Any]:
	cid = f"depth_{family}_w{width}_d{depth}_s{shots}_r{repetition}"
	ideal_circuit, circuit = build_family_circuit(family, width, depth, seed)
	mapping = {index: qubit for index, qubit in enumerate(physical_qubits)}
	ideal = ideal_probabilities(ideal_circuit, width, max_ideal_width)
	start = time.monotonic()
	try:
		if ctx.args.dry_run:
			qasm_files = ctx.write_qasm_artifacts(circuit, cid)
			error = min(max(dry_run_error * depth * max(width, 1), 0.0), 0.95)
			result = dry_run_result(
				cid,
				shots,
				distribution_counts(ideal, width, shots, error),
				execution_seconds=shots * (0.0004 + width * depth * 0.00003))
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
			"family": family,
			"width": width,
			"depth": depth,
			"physical_qubits": physical_qubits,
			"shots": shots,
			"repetition": repetition,
			"seed": seed,
			"job_id": run.job_id,
			"counts": run.counts or {},
			"metrics": {
				"script_wall_seconds": script_wall,
				"hellinger_fidelity": hellinger_fidelity(
					run.counts or {}, ideal or {}, width),
				"ideal_distribution_available": ideal is not None,
			},
			"files": run.files,
		}
	except Exception as exc:
		return {
			"ok": False,
			"family": family,
			"width": width,
			"depth": depth,
			"physical_qubits": physical_qubits,
			"shots": shots,
			"repetition": repetition,
			"seed": seed,
			"error": str(exc),
			"counts": {},
			"metrics": {},
			"files": {},
		}


def build_analysis(records: list[dict[str, Any]],
		   config: dict[str, Any]) -> dict[str, Any]:
	threshold = float(config["quality_threshold"])
	limits: dict[str, dict[str, Any]] = {}
	for record in records:
		if not record.get("ok"):
			continue
		value = record.get("metrics", {}).get("hellinger_fidelity")
		if value is None:
			continue
		key = f"{record['family']}:w{record['width']}"
		item = limits.setdefault(key, {
			"family": record["family"],
			"width": record["width"],
			"max_passing_depth": None,
			"points": [],
		})
		item["points"].append({
			"depth": record["depth"],
			"hellinger_fidelity": value,
		})
	for item in limits.values():
		passing = [
			point["depth"]
			for point in item["points"]
			if point["hellinger_fidelity"] >= threshold
		]
		item["max_passing_depth"] = max(passing) if passing else None
	return {
		"schema": "qhw-depth-limit-analysis-v1",
		"intent": (
			"Find the largest tested depth whose output distribution remains "
			"above the configured quality threshold."),
		"config": config,
		"record_count": len(records),
		"successful_record_count": sum(
			1 for record in records if record.get("ok")),
		"failed_record_count": sum(
			1 for record in records if not record.get("ok")),
		"limits": limits,
	}


def render_analysis_markdown(analysis: dict[str, Any]) -> str:
	lines = [
		"# Depth Limit Analysis",
		"",
		analysis["intent"],
		"",
		"| Family | Width | Max passing depth | Points |",
		"| --- | ---: | ---: | ---: |",
	]
	for item in analysis["limits"].values():
		lines.append(
			f"| `{item['family']}` | {item['width']} | "
			f"{item['max_passing_depth']} | {len(item['points'])} |")
	return "\n".join(lines) + "\n"


def add_script_args(parser: argparse.ArgumentParser) -> None:
	parser.add_argument("--qubits", default="all")
	parser.add_argument("--widths", default="1,2,4,max")
	parser.add_argument("--families", type=parse_families,
			    default=parse_families("ghz,mirror"))
	parser.add_argument("--depths", type=parse_int_list,
			    default=parse_int_list("1,2,4,8,16,32"))
	parser.add_argument("--shots", type=int, default=1000)
	parser.add_argument("--repetitions", type=int, default=1)
	parser.add_argument("--quality-threshold", type=float, default=0.5)
	parser.add_argument("--max-ideal-width", type=int, default=10)
	parser.add_argument("--sample-seed", type=int, default=23)
	parser.add_argument("--dry-run-error", type=float, default=0.002)


def main() -> int:
	ctx = WorkflowContext.from_cli(
		__file__,
		description="Sweep circuit families to estimate useful-depth limits.",
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
	widths = resolve_widths(args.widths, qubits)

	backend_info_file = ctx.paths.root / "backend_info.json"
	device_info_file = qhw_json_path(ctx.paths.root, "device_info")
	records_file = ctx.paths.results / "depth_records.jsonl"
	analysis_file = ctx.paths.results / "analysis.json"
	analysis_md_file = ctx.paths.results / "analysis.md"
	summary_file = ctx.paths.results / "depth_limits_summary.json"
	ctx.write_json(backend_info_file, backend_info)
	ctx.write_json(device_info_file, device_info)

	records = []
	for repetition in range(args.repetitions):
		for family in args.families:
			for width in widths:
				physical = qubits[:width]
				for depth in args.depths:
					seed = args.sample_seed + repetition + width * 1000 + depth
					records.append(run_record(
						ctx,
						family=family,
						width=width,
						depth=int(depth),
						physical_qubits=physical,
						shots=args.shots,
						repetition=repetition,
						seed=seed,
						max_ideal_width=args.max_ideal_width,
						dry_run_error=args.dry_run_error))

	write_jsonl(records_file, records)
	config = {
		"backend": ctx.backend_name,
		"qubits": qubits,
		"widths": widths,
		"families": args.families,
		"depths": args.depths,
		"shots": args.shots,
		"repetitions": args.repetitions,
		"quality_threshold": args.quality_threshold,
		"max_ideal_width": args.max_ideal_width,
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
