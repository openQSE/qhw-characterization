#!/usr/bin/env python3
"""Load an OpenQASM file as a Qiskit circuit and run it on a qhw backend."""

from __future__ import annotations

import argparse
import math
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from qhw_util.experiments import dry_run_result
from qhw_util.output import to_jsonable
from qhw_util.workflow import WorkflowContext


QASM_SUFFIXES = {".qasm", ".qasm2", ".qasm3"}


def strip_line_comment(line: str) -> str:
	return line.split("//", 1)[0]


def qasm_statements(text: str) -> list[str]:
	statements = []
	current = []
	clean_text = "\n".join(
		strip_line_comment(line) for line in text.splitlines())
	for char in clean_text:
		if char == ";":
			statement = "".join(current).strip()
			if statement:
				statements.append(statement)
			current = []
			continue
		current.append(char)
	if current:
		statement = "".join(current).strip()
		if statement:
			statements.append(statement)
	return statements


def qasm_version(text: str) -> str:
	for statement in qasm_statements(text):
		match = re.match(r"^OPENQASM\s+([0-9]+(?:\.[0-9]+)?)$", statement)
		if match:
			version = match.group(1)
			if version.startswith("3"):
				return "openqasm3"
			if version.startswith("2"):
				return "openqasm2"
	return "unknown"


def parse_angle(value: str) -> float:
	allowed = {"pi": math.pi}
	try:
		return float(eval(value, {"__builtins__": {}}, allowed))
	except Exception as exc:
		raise RuntimeError(f"invalid OpenQASM angle expression {value!r}") from exc


def parse_params(value: str | None) -> list[float]:
	if value is None or not value.strip():
		return []
	return [parse_angle(item.strip()) for item in value.split(",")]


def make_qasm3_circuit(path: Path, text: str):
	try:
		from qiskit import ClassicalRegister, QuantumCircuit, QuantumRegister
	except Exception as exc:
		raise RuntimeError("qiskit is required to load OpenQASM circuits") from exc

	statements = qasm_statements(text)
	qregs: dict[str, Any] = {}
	cregs: dict[str, Any] = {}
	registers = []

	for statement in statements:
		if statement.startswith("OPENQASM") or statement.startswith("include "):
			continue
		match = re.match(r"^qubit(?:\[(\d+)])?\s+([A-Za-z_]\w*)$", statement)
		if match:
			size = int(match.group(1) or "1")
			name = match.group(2)
			if name in qregs or name in cregs:
				raise RuntimeError(f"duplicate OpenQASM register {name!r}")
			qregs[name] = QuantumRegister(size, name)
			registers.append(qregs[name])
			continue
		match = re.match(r"^bit(?:\[(\d+)])?\s+([A-Za-z_]\w*)$", statement)
		if match:
			size = int(match.group(1) or "1")
			name = match.group(2)
			if name in qregs or name in cregs:
				raise RuntimeError(f"duplicate OpenQASM register {name!r}")
			cregs[name] = ClassicalRegister(size, name)
			registers.append(cregs[name])

	if not qregs:
		raise RuntimeError("OpenQASM 3 input does not define any qubits")

	circuit = QuantumCircuit(*registers, name=path.stem)

	def parse_ref(ref: str, regs: dict[str, Any], kind: str) -> list[Any]:
		ref = ref.strip()
		match = re.match(r"^([A-Za-z_]\w*)\[(\d+)]$", ref)
		if match:
			name = match.group(1)
			index = int(match.group(2))
			if name not in regs or index >= len(regs[name]):
				raise RuntimeError(f"unknown OpenQASM {kind} reference {ref!r}")
			return [regs[name][index]]
		if ref in regs:
			return [regs[ref][index] for index in range(len(regs[ref]))]
		raise RuntimeError(f"unknown OpenQASM {kind} reference {ref!r}")

	def parse_refs(refs: str, regs: dict[str, Any], kind: str) -> list[Any]:
		items = []
		for raw_ref in refs.split(","):
			ref = raw_ref.strip()
			if ref:
				items.extend(parse_ref(ref, regs, kind))
		if not items:
			raise RuntimeError(f"empty OpenQASM {kind} reference list")
		return items

	def measure(qrefs: str, crefs: str) -> None:
		qubits = parse_refs(qrefs, qregs, "qubit")
		clbits = parse_refs(crefs, cregs, "classical bit")
		if len(qubits) != len(clbits):
			raise RuntimeError(
				"OpenQASM measurement source and target sizes differ: "
				f"{qrefs!r} -> {crefs!r}")
		circuit.measure(qubits, clbits)

	def apply_gate(gate: str, params: list[float], qargs: list[Any]) -> None:
		if gate in {"x", "y", "z", "h", "s", "sdg", "t", "tdg", "sx", "sxdg", "id"}:
			if params:
				raise RuntimeError(f"OpenQASM gate {gate!r} takes no parameters")
			for qubit in qargs:
				getattr(circuit, gate)(qubit)
			return
		if gate in {"rx", "ry", "rz", "p"}:
			if len(params) != 1:
				raise RuntimeError(f"OpenQASM gate {gate!r} requires one parameter")
			for qubit in qargs:
				getattr(circuit, gate)(params[0], qubit)
			return
		if gate in {"cx", "cz"}:
			if params:
				raise RuntimeError(f"OpenQASM gate {gate!r} takes no parameters")
			if len(qargs) != 2:
				raise RuntimeError(f"OpenQASM gate {gate!r} requires two qubits")
			getattr(circuit, gate)(qargs[0], qargs[1])
			return
		raise RuntimeError(f"unsupported OpenQASM 3 gate {gate!r}")

	for statement in statements:
		if statement.startswith(("OPENQASM", "include ", "qubit", "bit")):
			continue
		if statement.startswith("barrier"):
			refs = statement[len("barrier"):].strip()
			circuit.barrier(*parse_refs(refs, qregs, "qubit"))
			continue
		if statement.startswith("reset"):
			refs = statement[len("reset"):].strip()
			for qubit in parse_refs(refs, qregs, "qubit"):
				circuit.reset(qubit)
			continue
		match = re.match(r"^(.+?)\s*=\s*measure\s+(.+)$", statement)
		if match:
			measure(match.group(2), match.group(1))
			continue
		match = re.match(r"^measure\s+(.+?)\s*->\s*(.+)$", statement)
		if match:
			measure(match.group(1), match.group(2))
			continue
		match = re.match(r"^([A-Za-z_]\w*)(?:\(([^)]*)\))?\s+(.+)$", statement)
		if match:
			gate = match.group(1)
			params = parse_params(match.group(2))
			qargs = parse_refs(match.group(3), qregs, "qubit")
			apply_gate(gate, params, qargs)
			continue
		raise RuntimeError(f"unsupported OpenQASM 3 statement: {statement}")

	return circuit


def load_qasm_circuit(path: Path) -> tuple[Any, dict[str, Any]]:
	text = path.read_text()
	version = qasm_version(text)
	try:
		if version == "openqasm3":
			from qiskit import qasm3
			try:
				return qasm3.load(str(path)), {
					"format": version,
					"loader": "qiskit.qasm3.load",
				}
			except Exception as exc:
				circuit = make_qasm3_circuit(path, text)
				return circuit, {
					"format": version,
					"loader": "qhw.openqasm3_subset",
					"qiskit_qasm3_error": str(exc),
				}

		from qiskit import QuantumCircuit, qasm2
		if hasattr(qasm2, "load"):
			return qasm2.load(str(path)), {
				"format": version if version != "unknown" else "openqasm2",
				"loader": "qiskit.qasm2.load",
			}
		return QuantumCircuit.from_qasm_file(str(path)), {
			"format": version if version != "unknown" else "openqasm2",
			"loader": "QuantumCircuit.from_qasm_file",
		}
	except Exception as exc:
		raise RuntimeError(f"failed to load OpenQASM circuit {path}: {exc}") from exc


def copy_source_qasm(source: Path, destination_dir: Path) -> Path:
	suffix = source.suffix if source.suffix in QASM_SUFFIXES else ".qasm"
	destination = destination_dir / f"source{suffix}"
	destination.write_bytes(source.read_bytes())
	return destination


def circuit_summary(circuit) -> dict[str, Any]:
	return {
		"name": circuit.name,
		"num_qubits": circuit.num_qubits,
		"num_clbits": circuit.num_clbits,
		"depth": circuit.depth(),
		"size": circuit.size(),
		"operations": dict(circuit.count_ops()),
	}


def parse_qubits(value: str | None, width: int) -> dict[int, str] | None:
	if value is None:
		return None
	qubits = [item.strip() for item in value.split(",") if item.strip()]
	if len(qubits) != width:
		raise argparse.ArgumentTypeError(
			f"--qubits must list exactly {width} physical qubits")
	return {index: qubit for index, qubit in enumerate(qubits)}


def maybe_transpile(circuit, ctx: WorkflowContext, enabled: bool):
	if not enabled:
		return circuit, None
	if ctx.backend is None:
		raise RuntimeError("--transpile requires a real backend")
	try:
		from qiskit import transpile
	except Exception as exc:
		raise RuntimeError("qiskit.transpile is required for transpilation") from exc

	qiskit_backend = ctx.backend.qiskit_backend(ctx.args.calibration_set_id)
	transpiled = transpile(
		circuit,
		backend=qiskit_backend,
		optimization_level=ctx.args.optimization_level,
	)
	return transpiled, circuit_summary(transpiled)


def add_script_args(parser: argparse.ArgumentParser) -> None:
	parser.add_argument("qasm_file", type=Path)
	parser.add_argument("--shots", type=int, default=100)
	parser.add_argument(
		"--name",
		default=None,
		help="Result and circuit artifact base name. Default: QASM file stem.",
	)
	parser.add_argument(
		"--qubits",
		default=None,
		help=(
			"Comma-separated physical qubit names for logical qubits "
			"0..N-1. Default: backend adapter chooses the mapping."
		),
	)
	parser.add_argument(
		"--transpile",
		action="store_true",
		help=(
			"Transpile the imported circuit for the selected Qiskit backend. "
			"Real backend runs do this by default."
		),
	)
	parser.add_argument(
		"--no-transpile",
		action="store_true",
		help="Submit the imported circuit without backend transpilation.",
	)
	parser.add_argument("--optimization-level", type=int, choices=range(4), default=1)


def main() -> int:
	ctx = WorkflowContext.from_cli(
		__file__,
		description="Import an OpenQASM file as a Qiskit circuit and run it.",
		add_args=add_script_args,
		calibration=True,
		execution=True,
		dry_run=True,
	)
	args = ctx.args
	qasm_file = args.qasm_file.expanduser().resolve()
	if not qasm_file.is_file():
		raise FileNotFoundError(f"QASM file does not exist: {qasm_file}")
	if args.shots < 1:
		raise ValueError("--shots must be at least 1")
	if args.transpile and args.no_transpile:
		raise ValueError("--transpile and --no-transpile are mutually exclusive")

	source_file = copy_source_qasm(qasm_file, ctx.paths.circuits)
	circuit, load_info = load_qasm_circuit(qasm_file)
	loaded_summary = circuit_summary(circuit)
	transpile_enabled = args.transpile or (
		not args.dry_run and not args.no_transpile)
	if transpile_enabled and args.qubits:
		raise ValueError(
			"--qubits cannot be combined with backend transpilation because "
			"backend transpilation may change the logical layout")
	qubit_mapping = parse_qubits(args.qubits, circuit.num_qubits)
	circuit, transpiled_summary = maybe_transpile(
		circuit, ctx, transpile_enabled)
	name = args.name or qasm_file.stem

	if args.dry_run:
		qasm_files = ctx.write_qasm_artifacts(circuit, name)
		run = ctx.write_backend_result(
			"result",
			dry_run_result(name, args.shots),
			qasm_files,
		)
	else:
		run = ctx.run_circuit(
			circuit,
			name="result",
			qasm_name=name,
			shots=args.shots,
			qubit_mapping=qubit_mapping,
		)

	input_file = ctx.write_input({
		"source": "openqasm",
		"qasm_file": str(qasm_file),
		"source_qasm_artifact": str(source_file),
		"qasm": load_info,
		"shots": args.shots,
		"name": name,
		"qubit_mapping": to_jsonable(qubit_mapping),
		"transpile": transpile_enabled,
		"transpile_requested": args.transpile,
		"no_transpile": args.no_transpile,
		"optimization_level": args.optimization_level,
		"loaded_circuit": loaded_summary,
		"submitted_circuit": transpiled_summary or circuit_summary(circuit),
		"calibration_set_id": args.calibration_set_id,
		"use_timeslot": args.use_timeslot,
		"submitted_qasm_artifact": run.files.get("qasm"),
	})
	timing_file = ctx.write_json(
		ctx.paths.results / "timing_summary.json", run.timing)

	summary = {
		"ok": run.ok,
		"backend_mode": ctx.backend_name,
		"job_id": run.job_id,
		"counts": run.counts,
		"qasm": load_info,
		"circuit": transpiled_summary or circuit_summary(circuit),
		"files": {
			"input": str(input_file),
			"source_qasm": str(source_file),
			**run.files,
			"timing_summary": str(timing_file),
			"script_output": str(ctx.script_output_file),
		},
	}
	lines = [
		f"run id: {ctx.paths.run_id}",
		f"output dir: {ctx.paths.root}",
		f"backend: {summary['backend_mode']}",
		f"loader: {load_info['loader']}",
		f"job id: {summary['job_id']}",
		f"counts: {summary['counts']}",
	]
	for name, path in summary["files"].items():
		lines.append(f"{name}: {path}")
	return ctx.finish(summary, ok=run.ok, text_lines=lines)


if __name__ == "__main__":
	raise SystemExit(main())
