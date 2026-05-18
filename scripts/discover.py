#!/usr/bin/env python3
"""Collect hardware discovery data through the selected backend."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from qhw_util.args import add_common_arguments
from qhw_util.backend import get_backend_from_args
from qhw_util.output import create_run_paths
from qhw_util.output import render_json_output
from qhw_util.output import render_text_output
from qhw_util.output import script_output_path
from qhw_util.output import to_jsonable
from qhw_util.output import write_json
from qhw_util.output import write_script_output
from qhw_util.schema import qhw_calibration_set_id
from qhw_util.schema import qhw_coupling_edges
from qhw_util.schema import qhw_coupling_nodes
from qhw_util.schema import qhw_device_qubits


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(
		description="Discover hardware architecture and calibration data.",
	)
	add_common_arguments(parser, calibration=True)
	return parser.parse_args()


def main() -> int:
	args = parse_args()
	paths = create_run_paths(__file__, args.output_dir, args.run_id)
	backend = get_backend_from_args(args)

	device_snapshot = {
		"backend_mode": backend.name,
		"backend_info": to_jsonable(backend.get_backend_info()),
		"device_info": to_jsonable(backend.get_device_info()),
		"dynamic_backend_info": to_jsonable(
			backend.get_dynamic_backend_info(args.calibration_set_id)),
	}
	calibration_snapshot = to_jsonable(
		backend.get_calibration_snapshot(args.calibration_set_id))
	coupling_graph = to_jsonable(
		backend.get_coupling_graph(args.calibration_set_id))

	files = {
		"device_snapshot": paths.root / "device_snapshot.json",
		"calibration_snapshot": paths.root / "calibration_snapshot.json",
		"coupling_graph": paths.root / "coupling_graph.json",
	}
	write_json(files["device_snapshot"], device_snapshot)
	files["device_snapshot_qhw"] = paths.root / "device_snapshot.qhw.json"
	write_json(files["device_snapshot_qhw"], device_snapshot["device_info"])
	write_json(files["calibration_snapshot"], calibration_snapshot)
	write_json(files["coupling_graph"], coupling_graph)

	qubits = qhw_device_qubits(device_snapshot["device_info"])
	nodes = qhw_coupling_nodes(coupling_graph)
	edges = qhw_coupling_edges(coupling_graph)
	summary = {
		"ok": True,
		"run_id": paths.run_id,
		"date_id": paths.date_id,
		"output_dir": str(paths.root),
		"files": {name: str(path) for name, path in files.items()},
		"qubits": len(qubits or nodes),
		"couplers": len(edges),
		"calibration_set_id": qhw_calibration_set_id(
			calibration_snapshot),
	}
	summary["files"]["script_output"] = str(
		script_output_path(paths, args.json))

	if args.json:
		output = render_json_output(summary)
	else:
		lines = [
			f"run id: {summary['run_id']}",
			f"output dir: {summary['output_dir']}",
			f"qubits: {summary['qubits']}",
			f"couplers: {summary['couplers']}",
			f"calibration set: {summary['calibration_set_id']}",
		]
		for name, path in summary["files"].items():
			lines.append(f"{name}: {path}")
		output = render_text_output(lines)
	write_script_output(paths, output, args.json)

	return backend.finish(0)


if __name__ == "__main__":
	raise SystemExit(main())
