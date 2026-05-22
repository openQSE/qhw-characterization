#!/usr/bin/env python3
"""Run two-qubit randomized benchmarking style circuits."""

from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from qhw_util.experiments import dry_run_result
from qhw_util.experiments import exponential_decay_fit
from qhw_util.experiments import parse_int_list
from qhw_util.experiments import success_probability
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


def parse_edge_list(value: str) -> list[list[str]]:
	edges = []
	for raw in value.split(","):
		item = raw.strip()
		if not item:
			continue
		for sep in ("-", ":", "/"):
			if sep in item:
				left, right = item.split(sep, 1)
				edges.append([left.strip(), right.strip()])
				break
		else:
			raise argparse.ArgumentTypeError(
				f"invalid edge {item!r}; use a form like QB1-QB2")
	return edges


def dry_run_counts(length: int, shots: int, error_per_clifford: float) -> dict[str, int]:
	survival = 0.25 + 0.75 * ((1.0 - error_per_clifford) ** length)
	success = int(round(shots * survival))
	rest = shots - success
	counts = {"00": success}
	if rest:
		share = rest // 3
		counts.update({"01": share, "10": share, "11": rest - 2 * share})
	return {key: value for key, value in counts.items() if value}


def build_rb_circuit(length: int, seed: int, name: str):
	try:
		from qiskit import QuantumCircuit
		from qiskit.quantum_info import Clifford
		from qiskit.quantum_info import random_clifford
	except Exception as exc:
		raise RuntimeError("qiskit is required for rb_2q.py") from exc

	body = QuantumCircuit(2, name=f"{name}_body")
	for index in range(length):
		clifford = random_clifford(2, seed=seed + index)
		body.compose(clifford.to_circuit(), inplace=True)
	inverse = Clifford(body).adjoint().to_circuit()

	circuit = QuantumCircuit(2, 2, name=name)
	circuit.compose(body, inplace=True)
	circuit.compose(inverse, inplace=True)
	circuit.measure([0, 1], [0, 1])
	return circuit


def run_record(ctx: WorkflowContext, *, edge: list[str], length: int,
	       sequence_index: int, shots: int, seed: int,
	       dry_run_error_per_clifford: float) -> dict[str, Any]:
	edge_key = f"{edge[0]}-{edge[1]}"
	cid = f"rb_2q_{edge_key}_l{length}_seq{sequence_index}_s{shots}"
	circuit = build_rb_circuit(length, seed, cid)
	mapping = {0: edge[0], 1: edge[1]}
	start = time.monotonic()
	try:
		if ctx.args.dry_run:
			qasm_files = ctx.write_qasm_artifacts(circuit, cid)
			result = dry_run_result(
				cid,
				shots,
				dry_run_counts(length, shots, dry_run_error_per_clifford),
				execution_seconds=shots * (0.0008 + length * 0.00008))
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
			"physical_edge": edge,
			"length": length,
			"sequence_index": sequence_index,
			"seed": seed,
			"shots": shots,
			"job_id": run.job_id,
			"counts": run.counts or {},
			"metrics": {
				"script_wall_seconds": script_wall,
				"survival_probability": success_probability(
					run.counts or {}, 2, [0, 0]),
			},
			"files": run.files,
		}
	except Exception as exc:
		return {
			"ok": False,
			"physical_edge": edge,
			"length": length,
			"sequence_index": sequence_index,
			"seed": seed,
			"shots": shots,
			"error": str(exc),
			"counts": {},
			"metrics": {},
			"files": {},
		}


def select_edges(coupling_graph: dict[str, Any], explicit_edges: list[list[str]],
		 max_edges: int, seed: int) -> list[list[str]]:
	if explicit_edges:
		edges = explicit_edges
	else:
		edges = qhw_coupling_edges(coupling_graph)
	if max_edges > 0 and len(edges) > max_edges:
		rng = random.Random(seed)
		edges = list(edges)
		rng.shuffle(edges)
		edges = edges[:max_edges]
	return edges


def build_analysis(records: list[dict[str, Any]],
		   config: dict[str, Any]) -> dict[str, Any]:
	by_edge: dict[str, dict[int, list[float]]] = {}
	for record in records:
		if not record.get("ok"):
			continue
		value = record.get("metrics", {}).get("survival_probability")
		if value is None:
			continue
		edge_key = "-".join(record["physical_edge"])
		by_edge.setdefault(edge_key, {}).setdefault(
			int(record["length"]), []).append(float(value))

	fits = {}
	for edge_key, lengths in sorted(by_edge.items()):
		points = [
			(float(length), sum(values) / len(values))
			for length, values in sorted(lengths.items())
		]
		contrast_points = [
			(length, max(value - 0.25, 0.0))
			for length, value in points
		]
		fit = exponential_decay_fit(contrast_points, floor=0.0)
		fits[edge_key] = {
			"points": [
				{"length": length, "survival_probability": value}
				for length, value in points
			],
			"decay_fit": fit,
			"estimated_error_per_clifford": (
				1.0 / fit["decay_constant"]
				if fit and fit.get("decay_constant") else None),
		}

	return {
		"schema": "qhw-rb-2q-analysis-v1",
		"intent": (
			"Estimate two-qubit RB survival decay across selected coupling "
			"graph edges."),
		"config": config,
		"record_count": len(records),
		"successful_record_count": sum(
			1 for record in records if record.get("ok")),
		"failed_record_count": sum(
			1 for record in records if not record.get("ok")),
		"fits": fits,
		"caveats": [
			"The circuits are Qiskit Clifford-authored two-qubit RB "
			"sequences and may be decomposed by provider transpilation.",
			"Use edge counts and sequence limits to avoid an unbounded run "
			"matrix on larger coupling graphs.",
		],
	}


def render_analysis_markdown(analysis: dict[str, Any]) -> str:
	lines = [
		"# 2Q Randomized Benchmarking Analysis",
		"",
		analysis["intent"],
		"",
		"| Edge | Estimated error per Clifford | Points |",
		"| --- | ---: | ---: |",
	]
	for edge, item in analysis["fits"].items():
		lines.append(
			f"| `{edge}` | {item['estimated_error_per_clifford']} | "
			f"{len(item['points'])} |")
	lines += ["", "## Caveats", ""]
	for caveat in analysis["caveats"]:
		lines.append(f"- {caveat}")
	return "\n".join(lines) + "\n"


def add_script_args(parser: argparse.ArgumentParser) -> None:
	parser.add_argument("--edges", type=parse_edge_list, default=[])
	parser.add_argument("--max-edges", type=int, default=4)
	parser.add_argument("--lengths", type=parse_int_list,
			    default=parse_int_list("1,2,4,8,16"))
	parser.add_argument("--sequences", type=int, default=4)
	parser.add_argument("--shots", type=int, default=1000)
	parser.add_argument("--sample-seed", type=int, default=17)
	parser.add_argument("--dry-run-error-per-clifford", type=float, default=0.018)


def main() -> int:
	ctx = WorkflowContext.from_cli(
		__file__,
		description="Run two-qubit randomized benchmarking circuits.",
		add_args=add_script_args,
		calibration=True,
		execution=True,
		dry_run=True,
	)
	args = ctx.args
	if args.shots < 1:
		raise ValueError("--shots must be at least 1")
	if args.sequences < 1:
		raise ValueError("--sequences must be at least 1")

	backend_info = {} if args.dry_run else to_jsonable(
		ctx.backend.get_backend_info())
	coupling_graph = (
		synthetic_coupling_graph()
		if args.dry_run
		else to_jsonable(ctx.backend.get_coupling_graph(args.calibration_set_id)))
	edges = select_edges(
		coupling_graph, args.edges, args.max_edges, args.sample_seed)
	if not edges:
		raise ValueError("no two-qubit edges were selected")

	backend_info_file = ctx.paths.root / "backend_info.json"
	coupling_file = qhw_json_path(ctx.paths.root, "coupling_graph")
	selected_edges_file = ctx.paths.root / "selected_edges.json"
	records_file = ctx.paths.results / "rb_2q_records.jsonl"
	analysis_file = ctx.paths.results / "analysis.json"
	analysis_md_file = ctx.paths.results / "analysis.md"
	summary_file = ctx.paths.results / "rb_2q_summary.json"
	ctx.write_json(backend_info_file, backend_info)
	ctx.write_json(coupling_file, coupling_graph)
	ctx.write_json(selected_edges_file, {"edges": edges})

	rng = random.Random(args.sample_seed)
	records = []
	for edge in edges:
		for length in args.lengths:
			for sequence_index in range(args.sequences):
				seed = rng.randrange(1, 2**31)
				records.append(run_record(
					ctx,
					edge=edge,
					length=int(length),
					sequence_index=sequence_index,
					shots=args.shots,
					seed=seed,
					dry_run_error_per_clifford=(
						args.dry_run_error_per_clifford)))

	write_jsonl(records_file, records)
	config = {
		"backend": ctx.backend_name,
		"edges": edges,
		"lengths": args.lengths,
		"sequences": args.sequences,
		"shots": args.shots,
		"sample_seed": args.sample_seed,
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
			"coupling_graph": str(coupling_file),
			"selected_edges": str(selected_edges_file),
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
