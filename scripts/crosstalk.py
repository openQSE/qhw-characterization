#!/usr/bin/env python3
"""Run bounded spectator crosstalk characterization circuits."""

from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from qhw_util.experiments import dry_run_result
from qhw_util.experiments import independent_readout_counts
from qhw_util.experiments import logical_one_probability
from qhw_util.experiments import write_jsonl
from qhw_util.output import qhw_json_path
from qhw_util.output import to_jsonable
from qhw_util.schema import qhw_coupling_edges
from qhw_util.workflow import WorkflowContext


def synthetic_coupling_graph() -> dict[str, Any]:
	nodes = [f"QB{index}" for index in range(1, 7)]
	edges = [["QB1", "QB2"], ["QB2", "QB3"], ["QB3", "QB4"]]
	return {
		"schema": "qhw-coupling-v1",
		"provider": "dry-run",
		"device": {"id": "dry-run", "provider": "dry-run"},
		"coupling": {"directed": False, "nodes": nodes, "edges": edges},
	}


def parse_pair_list(value: str) -> list[list[str]]:
	pairs = []
	for raw in value.split(","):
		item = raw.strip()
		if not item:
			continue
		for sep in ("-", ":", "/"):
			if sep in item:
				left, right = item.split(sep, 1)
				pairs.append([left.strip(), right.strip()])
				break
		else:
			raise argparse.ArgumentTypeError(
				f"invalid pair {item!r}; use a form like QB1-QB2")
	return pairs


def select_pairs(coupling_graph: dict[str, Any], explicit_pairs: list[list[str]],
		 max_pairs: int, bidirectional: bool, seed: int) -> list[list[str]]:
	pairs = explicit_pairs or qhw_coupling_edges(coupling_graph)
	if max_pairs > 0 and len(pairs) > max_pairs:
		pairs = list(pairs)
		random.Random(seed).shuffle(pairs)
		pairs = pairs[:max_pairs]
	if bidirectional:
		pairs = pairs + [[right, left] for left, right in pairs]
	return pairs


def build_circuit(experiment: str, spectator_state: int,
		  gate_depth: int, name: str):
	try:
		from qiskit import QuantumCircuit
	except Exception as exc:
		raise RuntimeError("qiskit is required for crosstalk.py") from exc

	circuit = QuantumCircuit(2, 2, name=name)
	if spectator_state:
		circuit.x(1)
	if experiment == "readout":
		pass
	elif experiment == "gate_echo":
		for _ in range(gate_depth):
			circuit.x(0)
			circuit.x(0)
	else:
		raise ValueError(f"unsupported experiment {experiment!r}")
	circuit.measure([0, 1], [0, 1])
	return circuit


def run_record(ctx: WorkflowContext, *, experiment: str, target: str,
	       spectator: str, spectator_state: int, gate_depth: int,
	       shots: int, repetition: int, dry_run_error: float,
	       dry_run_crosstalk: float) -> dict[str, Any]:
	cid = (
		f"crosstalk_{experiment}_{target}_{spectator}_"
		f"spec{spectator_state}_d{gate_depth}_s{shots}_r{repetition}")
	circuit = build_circuit(experiment, spectator_state, gate_depth, cid)
	mapping = {0: target, 1: spectator}
	start = time.monotonic()
	try:
		if ctx.args.dry_run:
			qasm_files = ctx.write_qasm_artifacts(circuit, cid)
			error = dry_run_error + spectator_state * dry_run_crosstalk
			counts = independent_readout_counts(
				[0, spectator_state], shots, error)
			result = dry_run_result(
				cid,
				shots,
				counts,
				execution_seconds=shots * (
					0.0004 + gate_depth * 0.00002))
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
			"experiment": experiment,
			"target_qubit": target,
			"spectator_qubit": spectator,
			"spectator_state": spectator_state,
			"gate_depth": gate_depth,
			"shots": shots,
			"repetition": repetition,
			"job_id": run.job_id,
			"counts": run.counts or {},
			"metrics": {
				"script_wall_seconds": script_wall,
				"target_error_probability": logical_one_probability(
					run.counts or {}, 2, 0),
			},
			"files": run.files,
		}
	except Exception as exc:
		return {
			"ok": False,
			"experiment": experiment,
			"target_qubit": target,
			"spectator_qubit": spectator,
			"spectator_state": spectator_state,
			"gate_depth": gate_depth,
			"shots": shots,
			"repetition": repetition,
			"error": str(exc),
			"counts": {},
			"metrics": {},
			"files": {},
		}


def build_analysis(records: list[dict[str, Any]],
		   config: dict[str, Any]) -> dict[str, Any]:
	grouped: dict[str, dict[int, list[float]]] = {}
	for record in records:
		if not record.get("ok"):
			continue
		value = record.get("metrics", {}).get("target_error_probability")
		if value is None:
			continue
		key = (
			f"{record['experiment']}:{record['target_qubit']}:"
			f"{record['spectator_qubit']}:d{record['gate_depth']}")
		grouped.setdefault(key, {}).setdefault(
			int(record["spectator_state"]), []).append(float(value))

	comparisons = {}
	for key, states in sorted(grouped.items()):
		base = sum(states.get(0, [])) / len(states[0]) if states.get(0) else None
		active = sum(states.get(1, [])) / len(states[1]) if states.get(1) else None
		comparisons[key] = {
			"target_error_spectator_0": base,
			"target_error_spectator_1": active,
			"delta": (
				active - base
				if active is not None and base is not None else None),
		}
	return {
		"schema": "qhw-crosstalk-analysis-v1",
		"intent": (
			"Compare target error while a coupled spectator is prepared in "
			"0 versus 1."),
		"config": config,
		"record_count": len(records),
		"successful_record_count": sum(
			1 for record in records if record.get("ok")),
		"failed_record_count": sum(
			1 for record in records if not record.get("ok")),
		"comparisons": comparisons,
		"flagged": {
			key: value
			for key, value in comparisons.items()
			if value["delta"] is not None
			and abs(value["delta"]) >= config["delta_threshold"]
		},
	}


def render_analysis_markdown(analysis: dict[str, Any]) -> str:
	lines = [
		"# Crosstalk Analysis",
		"",
		analysis["intent"],
		"",
		"| Case | Target error, spectator 0 | Target error, spectator 1 | Delta |",
		"| --- | ---: | ---: | ---: |",
	]
	for key, item in analysis["comparisons"].items():
		lines.append(
			f"| `{key}` | {item['target_error_spectator_0']} | "
			f"{item['target_error_spectator_1']} | {item['delta']} |")
	lines += [
		"",
		f"Flagged cases: {len(analysis['flagged'])}",
	]
	return "\n".join(lines) + "\n"


def add_script_args(parser: argparse.ArgumentParser) -> None:
	parser.add_argument("--pairs", type=parse_pair_list, default=[])
	parser.add_argument("--max-pairs", type=int, default=4)
	parser.add_argument("--bidirectional", action="store_true")
	parser.add_argument("--gate-depths", default="0,8")
	parser.add_argument("--shots", type=int, default=1000)
	parser.add_argument("--repetitions", type=int, default=1)
	parser.add_argument("--sample-seed", type=int, default=29)
	parser.add_argument("--delta-threshold", type=float, default=0.02)
	parser.add_argument("--dry-run-error", type=float, default=0.02)
	parser.add_argument("--dry-run-crosstalk", type=float, default=0.015)


def main() -> int:
	ctx = WorkflowContext.from_cli(
		__file__,
		description="Run bounded spectator crosstalk circuits.",
		add_args=add_script_args,
		calibration=True,
		execution=True,
		dry_run=True,
	)
	args = ctx.args
	gate_depths = [
		int(item.strip())
		for item in args.gate_depths.split(",")
		if item.strip()
	]
	if args.shots < 1:
		raise ValueError("--shots must be at least 1")
	if args.repetitions < 1:
		raise ValueError("--repetitions must be at least 1")
	if not gate_depths:
		raise ValueError("--gate-depths must contain at least one value")

	backend_info = {} if args.dry_run else to_jsonable(
		ctx.backend.get_backend_info())
	coupling_graph = (
		synthetic_coupling_graph()
		if args.dry_run
		else to_jsonable(ctx.backend.get_coupling_graph(args.calibration_set_id)))
	pairs = select_pairs(
		coupling_graph, args.pairs, args.max_pairs,
		args.bidirectional, args.sample_seed)
	if not pairs:
		raise ValueError("no crosstalk pairs were selected")

	backend_info_file = ctx.paths.root / "backend_info.json"
	coupling_file = qhw_json_path(ctx.paths.root, "coupling_graph")
	selected_pairs_file = ctx.paths.root / "selected_pairs.json"
	records_file = ctx.paths.results / "crosstalk_records.jsonl"
	analysis_file = ctx.paths.results / "analysis.json"
	analysis_md_file = ctx.paths.results / "analysis.md"
	summary_file = ctx.paths.results / "crosstalk_summary.json"
	ctx.write_json(backend_info_file, backend_info)
	ctx.write_json(coupling_file, coupling_graph)
	ctx.write_json(selected_pairs_file, {"pairs": pairs})

	records = []
	for repetition in range(args.repetitions):
		for target, spectator in pairs:
			for gate_depth in gate_depths:
				experiment = "readout" if gate_depth == 0 else "gate_echo"
				for spectator_state in (0, 1):
					records.append(run_record(
						ctx,
						experiment=experiment,
						target=target,
						spectator=spectator,
						spectator_state=spectator_state,
						gate_depth=gate_depth,
						shots=args.shots,
						repetition=repetition,
						dry_run_error=args.dry_run_error,
						dry_run_crosstalk=args.dry_run_crosstalk))

	write_jsonl(records_file, records)
	config = {
		"backend": ctx.backend_name,
		"pairs": pairs,
		"gate_depths": gate_depths,
		"shots": args.shots,
		"repetitions": args.repetitions,
		"delta_threshold": args.delta_threshold,
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
		"flagged_cases": len(analysis["flagged"]),
		"files": {
			"backend_info": str(backend_info_file),
			"coupling_graph": str(coupling_file),
			"selected_pairs": str(selected_pairs_file),
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
			f"flagged cases: {summary['flagged_cases']}",
			f"output: {ctx.paths.root}",
		],
	)


if __name__ == "__main__":
	raise SystemExit(main())
