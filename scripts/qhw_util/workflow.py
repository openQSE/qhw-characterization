"""Workflow-level helpers for qhw test scripts.

This module intentionally stays thin. It only bundles the common script
plumbing that every workflow otherwise repeats: argument parsing, output
directory creation, backend selection, circuit execution, and artifact writes.
The test-specific logic should still live in the individual scripts.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from qhw_util.args import add_common_arguments
from qhw_util.backend import get_backend_from_args
from qhw_util.output import RunPaths
from qhw_util.output import backend_result_qhw
from qhw_util.output import backend_result_raw
from qhw_util.output import create_run_paths
from qhw_util.output import render_json_output
from qhw_util.output import render_text_output
from qhw_util.output import script_output_path
from qhw_util.output import to_jsonable
from qhw_util.output import write_backend_result_artifacts
from qhw_util.output import write_json
from qhw_util.output import write_script_output
from qhw_util.qiskit_exec import ensure_circuit_list
from qhw_util.qiskit_exec import write_qasm2_artifact


AddArgsCallback = Callable[[argparse.ArgumentParser], None]


@dataclass(frozen=True)
class WorkflowCircuitResult:
	"""Result bundle returned by WorkflowContext.run_circuit()."""

	result: dict[str, Any]
	qhw: dict[str, Any]
	raw: dict[str, Any]
	files: dict[str, Any]

	@property
	def ok(self) -> bool:
		return self.result.get("rc") == 0

	@property
	def job_id(self) -> str | None:
		qhw_job = self.qhw.get("job", {})
		if isinstance(qhw_job, dict) and qhw_job.get("id"):
			return str(qhw_job["id"])
		cid = self.result.get("cid")
		return str(cid) if cid else None

	@property
	def counts(self) -> Any:
		qhw_payload = self.qhw.get("result", {})
		if isinstance(qhw_payload, dict):
			return qhw_payload.get("counts")
		return None

	@property
	def timing(self) -> dict[str, Any]:
		timing = self.qhw.get("timing")
		return timing if isinstance(timing, dict) else {}


@dataclass
class WorkflowContext:
	"""Common runtime context for a qhw test script."""

	args: argparse.Namespace
	paths: RunPaths
	backend: Any

	@property
	def script_output_file(self) -> Path:
		return script_output_path(self.paths, self.args.json)

	@property
	def backend_name(self) -> str:
		if self.backend is None:
			return self.args.backend
		return self.backend.name

	@classmethod
	def from_cli(cls, script_file: str, *,
		     description: str,
		     add_args: AddArgsCallback | None = None,
		     argv: Sequence[str] | None = None,
		     **common_argument_options) -> "WorkflowContext":
		parser = argparse.ArgumentParser(description=description)
		if add_args is not None:
			add_args(parser)
		add_common_arguments(parser, **common_argument_options)
		args = parser.parse_args(argv)
		paths = create_run_paths(script_file, args.output_dir, args.run_id)
		backend = (
			None
			if getattr(args, "dry_run", False)
			else get_backend_from_args(args))
		return cls(args=args, paths=paths, backend=backend)

	def write_json(self, path: Path, data: Any) -> Path:
		write_json(path, data)
		return path

	def write_input(self, data: Any, name: str = "input.json") -> Path:
		return self.write_json(self.paths.root / name, data)

	def _default_arg(self, name: str, default: Any = None) -> Any:
		return getattr(self.args, name, default)

	def write_qasm_artifacts(self, circuits, qasm_name: str) -> list[str]:
		circuit_list = ensure_circuit_list(circuits)
		qasm_paths = []
		for index, circuit in enumerate(circuit_list):
			if len(circuit_list) == 1:
				qasm_path = self.paths.circuits / f"{qasm_name}.qasm"
			else:
				qasm_path = self.paths.circuits / f"{qasm_name}_{index}.qasm"
			write_qasm2_artifact(circuit, qasm_path)
			qasm_paths.append(str(qasm_path))
		return qasm_paths

	def write_backend_result(self, name: str, result: Any,
				 qasm_files: list[str] | None = None
				 ) -> WorkflowCircuitResult:
		result = to_jsonable(result)
		result_file = self.paths.results / f"{name}.json"
		result_files = write_backend_result_artifacts(result_file, result)

		qhw_result = backend_result_qhw(result)
		if not qhw_result:
			raise ValueError(
				"backend circuit result did not include normalized qhw_result")

		files: dict[str, Any] = {
			"result": result_files.get("qhw"),
			"normalized_result": result_files.get("qhw"),
		}
		if result_files.get("raw"):
			files["raw_result"] = result_files["raw"]
		if qasm_files:
			files["qasm"] = qasm_files[0] if len(qasm_files) == 1 else qasm_files

		return WorkflowCircuitResult(
			result=result,
			qhw=qhw_result,
			raw=backend_result_raw(result),
			files=files,
		)

	def run_circuit(self, circuits, *,
		    name: str = "result",
		    qasm_name: str | None = None,
		    shots: int | None = None,
		    calibration_set_id: str | None = None,
		    timeout: float | None = None,
		    use_timeslot: bool | None = None,
		    qubit_mapping=None,
		    write_qasm: bool = True,
		    **run_options) -> WorkflowCircuitResult:
		shots = shots if shots is not None else self._default_arg("shots", 100)
		calibration_set_id = (
			calibration_set_id
			if calibration_set_id is not None
			else self._default_arg("calibration_set_id"))
		timeout = timeout if timeout is not None else self._default_arg("timeout")
		use_timeslot = (
			use_timeslot
			if use_timeslot is not None
			else self._default_arg("use_timeslot", False))

		qasm_files = []
		if write_qasm:
			qasm_files = self.write_qasm_artifacts(
				circuits, qasm_name or name)

		if self.backend is None:
			raise RuntimeError("cannot run a circuit without a backend")

		job = self.backend.run(
			circuits,
			shots=shots,
			calibration_set_id=calibration_set_id,
			timeout=timeout,
			use_timeslot=use_timeslot,
			qubit_mapping=qubit_mapping,
			**run_options)
		result = job.result(timeout=timeout)
		return self.write_backend_result(name, result, qasm_files)

	def finish(self, summary: dict[str, Any], *,
	       ok: bool = True,
	       text_lines: list[str] | None = None,
	       success_rc: int = 0,
	       failure_rc: int = 2) -> int:
		payload = to_jsonable(dict(summary))
		payload.setdefault("run_id", self.paths.run_id)
		payload.setdefault("date_id", self.paths.date_id)
		payload.setdefault("output_dir", str(self.paths.root))
		files = payload.setdefault("files", {})
		if isinstance(files, dict):
			files.setdefault("script_output", str(self.script_output_file))

		if self.args.json:
			output = render_json_output(payload)
		elif text_lines is not None:
			output = render_text_output(text_lines)
		else:
			output = render_json_output(payload)
		write_script_output(self.paths, output, self.args.json)

		rc = success_rc if ok else failure_rc
		return rc if self.backend is None else self.backend.finish(rc)
