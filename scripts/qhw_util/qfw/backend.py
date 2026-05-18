"""QFw-backed workflow adapter."""

from __future__ import annotations

from time import sleep

from qhw_util.qiskit_exec import qiskit_result_metadata


def _selector_values(values, default):
	if values is None:
		values = default
	if isinstance(values, str):
		values = [values]
	result = []
	for value in values:
		for item in str(value).split(","):
			item = item.strip()
			if item:
				result.append(item)
	return result


def _enum_flag(enum_type, prefix: str, values, default):
	items = _selector_values(values, default)
	if not items or any(item.lower() in ("any", "-1") for item in items):
		return -1

	flag = enum_type(0)
	for item in items:
		name = item.upper().replace("-", "_")
		if not name.startswith(f"{prefix}_"):
			name = f"{prefix}_{name}"
		try:
			flag |= getattr(enum_type, name)
		except AttributeError as exc:
			valid = ", ".join(enum_type.__members__)
			raise ValueError(
				f"unsupported QFw selector {item!r}; valid values: "
				f"{valid}") from exc
	return flag


class QFwBackend:
	name = "qfw"

	def __init__(self, system_up_timeout: int = 40, qfw_type=None,
		     qfw_capabilities=None):
		self._system_up_timeout = system_up_timeout
		self._qiskit_backend = None
		self._qpm_ready = False
		self._qfw_type = qfw_type
		self._qfw_capabilities = qfw_capabilities

	def _service(self):
		qpm = self.qiskit_backend().qpm
		if self._qpm_ready:
			return qpm
		from defw_exception import DEFwNotReady
		waited = 0
		while waited < self._system_up_timeout:
			try:
				qpm.is_ready()
				self._qpm_ready = True
				return qpm
			except Exception as exc:
				if isinstance(exc, DEFwNotReady):
					sleep(1)
					waited += 1
					continue
				raise
		raise TimeoutError("selected QFw QPM did not become ready")

	def get_backend_info(self):
		return self._service().get_backend_info()

	def get_device_info(self):
		return self._service().get_device_info()

	def get_dynamic_backend_info(self, calibration_set_id=None):
		return self._service().get_dynamic_backend_info(calibration_set_id)

	def get_calibration_snapshot(self, calibration_set_id=None):
		return self._service().get_calibration_snapshot(calibration_set_id)

	def get_coupling_graph(self, calibration_set_id=None):
		return self._service().get_coupling_graph(calibration_set_id)

	def set_qubit_mapping(self, circuit, mapping):
		return self.qiskit_backend().set_qubit_mapping(circuit, mapping)

	def sync_run(self, info):
		return self._service().sync_run(info)

	def sync_run_many(self, infos):
		results = []
		for info in infos:
			results.append(self.sync_run(info))
		return {
			"cid": "qfw-sequential-batch",
			"result": {
				"batch_semantics": "sequential-qfw-sync-run",
				"results": results,
			},
			"rc": 0,
		}

	def qiskit_backend(self, calibration_set_id=None):
		del calibration_set_id
		if self._qiskit_backend is None:
			from qfw_qiskit import QFwBackend as QFwQiskitBackend
			from qfw_qiskit import QFwBackendCapability
			from qfw_qiskit import QFwBackendType
			self._qiskit_backend = QFwQiskitBackend(
				betype=_enum_flag(
					QFwBackendType, "QFW_TYPE",
					self._qfw_type, "hardware"),
				capability=_enum_flag(
					QFwBackendCapability, "QFW_CAP",
					self._qfw_capabilities, "superconducting"))
		return self._qiskit_backend

	def qiskit_run_options(self, shots: int, calibration_set_id=None,
	    timeout=None, use_timeslot=False, qubit_mapping=None,
	    extra_run_options=None):
		del calibration_set_id, timeout, use_timeslot, qubit_mapping
		options = {"shots": shots}
		options.update(extra_run_options or {})
		return options

	def qiskit_job_result(self, job, timeout=None):
		del timeout
		return job.result()

	def qiskit_record_extra(self, context):
		return {
			"qfw": {
				"calibration_set_id": context.get("calibration_set_id"),
				"timeout_requested": context.get("timeout"),
				"use_timeslot_requested": context.get("use_timeslot"),
				"qubit_mapping": context.get("qubit_mapping"),
			},
		}

	def extract_result_and_normalize(self, job, result, record, context):
		del job, result, context
		result_dict = record.get("result", {}).get("qiskit", {}).get("result")
		metadata = qiskit_result_metadata(result_dict or {})
		qhw_results = [
			item.get("qhw_result") for item in metadata
			if isinstance(item.get("qhw_result"), dict)
		]
		if not qhw_results:
			raise RuntimeError(
				"QFw result did not include normalized qhw_result "
				"metadata. Check that the selected QFw service returns "
				"qhw-normalized result payloads.")

		if len(qhw_results) == 1:
			record.setdefault("result", {})["qhw_result"] = qhw_results[0]
			return record

		record.setdefault("result", {})["qhw_results"] = qhw_results
		record.setdefault("result", {})["qhw_result"] = qhw_results[0]
		return record

	def run_circuits(self, circuits, shots: int = 100,
		     calibration_set_id=None, timeout=None, use_timeslot=False):
		from qhw_util.backend import BackendWrapper
		return BackendWrapper(self).run_circuits(
			circuits,
			shots=shots,
			calibration_set_id=calibration_set_id,
			timeout=timeout,
			use_timeslot=use_timeslot,
		)

	def finish(self, rc: int = 0) -> int:
		from qhw_util.qfw.runtime import finish
		return finish(rc)
