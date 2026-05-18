"""Backend selection for hardware test workflows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import os
import time

from qhw_util.args import BACKEND_CHOICES
from qhw_util.qiskit_exec import build_qiskit_run_record
from qhw_util.qiskit_exec import ensure_circuit_list


def qfw_available() -> bool:
	if not os.environ.get("QFW_PATH") or not os.environ.get("QFW_SETUP_PATH"):
		return False
	try:
		import api_qpm  # noqa: F401
		import defw_app_util  # noqa: F401
	except Exception:
		return False
	return True


@dataclass
class BackendJob:
	wrapper: "BackendWrapper"
	qiskit_job: Any
	circuit_list: list[Any]
	shots: int
	run_start: float
	context: dict[str, Any]

	def result(self, timeout=None):
		result_timeout = timeout
		if result_timeout is None:
			result_timeout = self.context.get("timeout")
		result = self.wrapper._job_result(
			self.qiskit_job, timeout=result_timeout)
		record = build_qiskit_run_record(
			self.wrapper.name,
			self.circuit_list,
			self.shots,
			self.run_start,
			self.qiskit_job,
			result,
			extra=self.wrapper._record_extra(self.context),
		)
		return self.wrapper._extract_result_and_normalize(
			self.qiskit_job, result, record, self.context)


class BackendWrapper:
	def __init__(self, adapter):
		self._adapter = adapter

	@property
	def name(self):
		return self._adapter.name

	def __getattr__(self, name):
		return getattr(self._adapter, name)

	def run(self, circuits, shots: int = 100, calibration_set_id=None,
	    timeout=None, use_timeslot=False, **kwargs):
		circuit_list = ensure_circuit_list(circuits)
		run_input = circuit_list[0] if len(circuit_list) == 1 else circuit_list
		context = {
			"shots": shots,
			"calibration_set_id": calibration_set_id,
			"timeout": timeout,
			"use_timeslot": use_timeslot,
			"extra_run_options": kwargs,
		}
		qiskit_backend = self._adapter.qiskit_backend(calibration_set_id)
		run_options = self._run_options(context)
		run_start = time.monotonic()
		qiskit_job = qiskit_backend.run(run_input, **run_options)
		return BackendJob(
			self, qiskit_job, circuit_list, shots, run_start, context)

	def run_circuits(self, circuits, shots: int = 100, calibration_set_id=None,
	    timeout=None, use_timeslot=False, **kwargs):
		return self.run(
			circuits,
			shots=shots,
			calibration_set_id=calibration_set_id,
			timeout=timeout,
			use_timeslot=use_timeslot,
			**kwargs,
		).result(timeout=timeout)

	def finish(self, rc: int = 0) -> int:
		return self._adapter.finish(rc)

	def _run_options(self, context: dict[str, Any]) -> dict[str, Any]:
		if hasattr(self._adapter, "qiskit_run_options"):
			return self._adapter.qiskit_run_options(**context)
		options = {"shots": context["shots"]}
		options.update(context.get("extra_run_options") or {})
		return options

	def _job_result(self, job, timeout=None):
		if hasattr(self._adapter, "qiskit_job_result"):
			return self._adapter.qiskit_job_result(job, timeout=timeout)
		return job.result(timeout=timeout)

	def _record_extra(self, context: dict[str, Any]) -> dict[str, Any] | None:
		if hasattr(self._adapter, "qiskit_record_extra"):
			return self._adapter.qiskit_record_extra(context)
		return None

	def _extract_result_and_normalize(self, job, result, record, context):
		return self._adapter.extract_result_and_normalize(
			job=job,
			result=result,
			record=record,
			context=context,
		)


def get_backend(mode: str = "auto", system_up_timeout: int = 40,
		provider: str = "iqm", qfw_type=None, qfw_capabilities=None):
	if mode not in BACKEND_CHOICES:
		raise ValueError(f"invalid backend mode {mode!r}")

	if mode == "qfw":
		if not qfw_available():
			raise RuntimeError(
				"QFw backend was requested, but QFw is not available. "
				"Source qfw_activate and run through qfw_srun.sh.")
		from qhw_util.qfw.backend import QFwBackend
		return BackendWrapper(QFwBackend(
			system_up_timeout=system_up_timeout,
			qfw_type=qfw_type,
			qfw_capabilities=qfw_capabilities))

	if mode == "auto" and qfw_available():
		from qhw_util.qfw.backend import QFwBackend
		return BackendWrapper(QFwBackend(
			system_up_timeout=system_up_timeout,
			qfw_type=qfw_type,
			qfw_capabilities=qfw_capabilities))

	if mode == "auto" or mode == "direct":
		if provider != "iqm":
			raise RuntimeError(
				f"unsupported direct provider {provider!r}; only iqm "
				"is implemented")
		from qhw_util.iqm.backend import DirectIQMBackend
		return BackendWrapper(DirectIQMBackend())

	raise RuntimeError(f"unsupported backend mode {mode!r}")


def get_backend_from_args(args):
	return get_backend(
		args.backend,
		system_up_timeout=args.system_up_timeout,
		provider=getattr(args, "provider", "iqm"),
		qfw_type=getattr(args, "qfw_type", None),
		qfw_capabilities=getattr(args, "qfw_capability", None),
	)
