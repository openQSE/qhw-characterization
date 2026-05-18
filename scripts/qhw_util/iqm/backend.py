"""Direct iqm-client backend for IQM workflows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID
import inspect
import math
import os
import re
import time

from qhw_util.iqm.qhw import QHW_IQM_DEVICE_ID_KEY, QHW_IQM_KIND_KEY
from qhw_util.iqm.qhw import normalize_iqm_payload, qhw_device_id
from qhw_util.output import to_jsonable
from qhw_util.qiskit_exec import optional_attr_data

REQUIRED_ENV = ("QFW_QC_URL", "QFW_API_KEY")
DEFAULT_REQUEST_TIMEOUT = 30.0
DEFAULT_JOB_TIMEOUT = 300.0


@dataclass(frozen=True)
class EnvConfig:
	url: str
	api_key: str
	quantum_computer: str | None = None


def sanitize_url(url: str) -> str:
	parsed = urlsplit(url)
	if parsed.username or parsed.password:
		netloc = parsed.hostname or ""
		if parsed.port:
			netloc = f"{netloc}:{parsed.port}"
		parsed = parsed._replace(netloc=netloc)
	return urlunsplit(parsed)


def get_env_float(name: str, default: float) -> float:
	value = os.environ.get(name)
	if not value:
		return default
	try:
		return float(value)
	except ValueError as exc:
		raise RuntimeError(f"{name} must be a float: {value!r}") from exc


def load_env() -> EnvConfig:
	missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
	if missing:
		raise RuntimeError(
			"missing required IQM environment variable(s): "
			f"{', '.join(missing)}")
	return EnvConfig(
		url=os.environ["QFW_QC_URL"].strip(),
		api_key=os.environ["QFW_API_KEY"].strip(),
		quantum_computer=os.environ.get("QHW_IQM_QUANTUM_COMPUTER"),
	)


def load_iqm_client_module():
	try:
		from iqm.iqm_client import IQMClient
	except Exception as exc:
		raise RuntimeError(
			"failed to import iqm-client. Install iqm-client before "
			f"running direct IQM mode. Import error: {exc}") from exc
	return IQMClient


def load_iqm_pulse_module():
	try:
		from iqm.pulse import Circuit, CircuitOperation
	except Exception as exc:
		raise RuntimeError(
			f"failed to import iqm.pulse circuit objects: {exc}") from exc
	return Circuit, CircuitOperation


def method_accepts(method: Callable[..., Any], name: str) -> bool:
	try:
		signature = inspect.signature(method)
	except (TypeError, ValueError):
		return True
	return name in signature.parameters


def call_iqm_method(method: Callable[..., Any],
		    timeout: float,
		    *args: Any,
		    **kwargs: Any) -> Any:
	call_kwargs = dict(kwargs)
	if method_accepts(method, "timeout_secs"):
		call_kwargs["timeout_secs"] = timeout
	return method(*args, **call_kwargs)


def parse_calibration_set_id(value):
	if not value:
		return None
	if isinstance(value, UUID):
		return value
	try:
		return UUID(str(value))
	except ValueError as exc:
		raise RuntimeError(
			f"invalid IQM calibration set id {value!r}: {exc}") from exc


def fetch_iqm_section(name: str, func: Callable[[], Any]) -> dict[str, Any]:
	try:
		return {
			"name": name,
			"ok": True,
			"data": to_jsonable(func()),
			"error": None,
		}
	except Exception as exc:
		return {
			"name": name,
			"ok": False,
			"data": {},
			"error": str(exc),
		}


def summarize_observation_set(data):
	if not isinstance(data, dict):
		data = {}
	observations = data.get("observations", {})
	if not isinstance(observations, dict):
		observations = {}
	return {
		"calibration_set_id": (
			data.get("calibration_set_id")
			or data.get("id")
			or data.get("observation_set_id")),
		"observation_count": len(observations),
		"observation_names": sorted(str(name) for name in observations.keys()),
	}


def create_iqm_client(client_type, config: EnvConfig):
	kwargs = {"token": config.api_key}
	if config.quantum_computer and method_accepts(
			client_type, "quantum_computer"):
		kwargs["quantum_computer"] = config.quantum_computer
	return client_type(config.url, **kwargs)


def submit_run_request(client, run_request, use_timeslot: bool):
	submit = client.submit_run_request
	if method_accepts(submit, "use_timeslot"):
		return submit(run_request, use_timeslot=use_timeslot)
	if use_timeslot:
		raise RuntimeError(
			"this iqm-client version does not support use_timeslot")
	return submit(run_request)


def normalize_status(status) -> str:
	if hasattr(status, "value"):
		return str(status.value)
	return str(status)


def get_dynamic_qubits(data):
	qubits = data.get("qubits") or []
	if isinstance(qubits, dict):
		return list(qubits.keys())
	return qubits


def get_dynamic_couplers(data):
	couplers = data.get("couplers") or []
	if isinstance(couplers, dict):
		return list(couplers.values())
	return couplers


def normalize_locus(value: Any) -> list[str]:
	if isinstance(value, str):
		return [part.strip() for part in value.split(",") if part.strip()]
	if isinstance(value, (list, tuple)):
		return [str(part) for part in value]
	return []


def normalize_edge(locus: list[str]) -> tuple[str, str] | None:
	if len(locus) != 2:
		return None
	a, b = locus
	if a == b:
		return None
	return tuple(sorted((a, b)))


def sorted_edges(edges: set[tuple[str, str]]) -> list[list[str]]:
	return [list(edge) for edge in sorted(edges)]


def collect_static_component_edges(static_arch) -> set[tuple[str, str]]:
	edges = set()
	for item in static_arch.get("connectivity", []):
		edge = normalize_edge(normalize_locus(item))
		if edge:
			edges.add(edge)
	return edges


def collect_gate_loci(dynamic_arch) -> dict[str, list[list[str]]]:
	gate_loci = {}
	gates = dynamic_arch.get("gates", {})
	if not isinstance(gates, dict):
		return gate_loci

	for gate_name, gate_info in gates.items():
		loci = set()
		if isinstance(gate_info, dict):
			implementations = gate_info.get("implementations", {})
			if isinstance(implementations, dict):
				for implementation in implementations.values():
					if not isinstance(implementation, dict):
						continue
					for locus in implementation.get("loci", []):
						normalized = tuple(normalize_locus(locus))
						if normalized:
							loci.add(normalized)
		gate_loci[str(gate_name)] = [
			list(locus) for locus in sorted(loci)
		]
	return gate_loci


def build_coupling_graph(static_arch, dynamic_arch) -> dict[str, Any]:
	qubits = sorted(str(q) for q in dynamic_arch.get("qubits")
			or static_arch.get("qubits", []))
	resonators = sorted(str(r) for r in dynamic_arch.get(
		"computational_resonators",
	) or static_arch.get("computational_resonators", []))
	qubit_set = set(qubits)
	component_edges = collect_static_component_edges(static_arch)
	gate_loci = collect_gate_loci(dynamic_arch)

	qubit_edges = set()
	gate_edges = {}
	for gate_name, loci in gate_loci.items():
		edges = set()
		for locus in loci:
			edge = normalize_edge(locus)
			if edge and edge[0] in qubit_set and edge[1] in qubit_set:
				edges.add(edge)
				qubit_edges.add(edge)
		if edges:
			gate_edges[gate_name] = sorted_edges(edges)

	if not qubit_edges:
		for edge in component_edges:
			if edge[0] in qubit_set and edge[1] in qubit_set:
				qubit_edges.add(edge)

	return {
		"qubits": qubits,
		"computational_resonators": resonators,
		"component_edges": sorted_edges(component_edges),
		"qubit_edges": sorted_edges(qubit_edges),
		"couplers": sorted_edges(qubit_edges),
		"gate_loci": gate_loci,
		"gate_edges": gate_edges,
		"source_priority": [
			"dynamic_architecture.gates.*.implementations.*.loci",
			"static_architecture.connectivity",
		],
	}


def active_qubits(dynamic_architecture):
	data = to_jsonable(dynamic_architecture)
	qubits = data.get("qubits") or []
	if not qubits:
		raise RuntimeError(
			"IQM dynamic architecture did not report active qubits")
	return [str(qubit) for qubit in qubits]


def eval_angle(value):
	allowed = {"pi": math.pi}
	try:
		return float(eval(value, {"__builtins__": {}}, allowed))
	except Exception as exc:
		raise RuntimeError(
			f"unsupported angle expression in OpenQASM: {value!r}") from exc


def split_qasm_statements(qasm):
	statements = []
	for line in qasm.splitlines():
		line = line.split("//", 1)[0].strip()
		if not line:
			continue
		for part in line.split(";"):
			part = part.strip()
			if part:
				statements.append(part)
	return statements


def parse_ref(ref, qregs):
	ref = ref.strip()
	match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\[(\d+)\]$", ref)
	if match:
		reg, index = match.group(1), int(match.group(2))
		if reg not in qregs or index >= qregs[reg]:
			raise RuntimeError(f"unknown OpenQASM qubit reference {ref}")
		return [(reg, index)]
	if ref in qregs:
		return [(ref, index) for index in range(qregs[ref])]
	raise RuntimeError(f"unknown OpenQASM qubit reference {ref}")


def build_manual_iqm_circuit(qasm, dynamic_architecture, mapping):
	Circuit, CircuitOperation = load_iqm_pulse_module()
	statements = split_qasm_statements(qasm)
	qregs = {}
	cregs = {}
	operations = []

	for statement in statements:
		if statement.startswith("OPENQASM") or statement.startswith("include"):
			continue
		match = re.match(r"^qreg\s+(\w+)\[(\d+)\]$", statement)
		if match:
			qregs[match.group(1)] = int(match.group(2))
			continue
		match = re.match(r"^creg\s+(\w+)\[(\d+)\]$", statement)
		if match:
			cregs[match.group(1)] = int(match.group(2))
			continue

	if not qregs:
		raise RuntimeError("OpenQASM input does not define a qreg")

	total_qubits = sum(qregs.values())
	physical = active_qubits(dynamic_architecture)
	if total_qubits > len(physical):
		raise RuntimeError(
			f"circuit requires {total_qubits} qubits but IQM reports "
			f"{len(physical)} active qubits")

	ordered = []
	for reg, size in qregs.items():
		for index in range(size):
			ordered.append((reg, index))
	if mapping:
		if isinstance(mapping, dict):
			qubit_map = {
				key: str(mapping.get(
					f"{key[0]}[{key[1]}]",
					mapping.get(key[1], mapping.get(str(key[1])))))
				for key in ordered
			}
		else:
			qubit_map = {
				key: str(mapping[index])
				for index, key in enumerate(ordered)
			}
	else:
		qubit_map = {
			key: physical[index] for index, key in enumerate(ordered)
		}

	for statement in statements:
		if statement.startswith(("OPENQASM", "include", "qreg", "creg")):
			continue
		if statement.startswith("barrier"):
			refs = statement[len("barrier"):].strip()
			locus = []
			for ref in refs.split(","):
				locus.extend(qubit_map[key] for key in parse_ref(ref, qregs))
			operations.append(CircuitOperation(
				name="barrier", locus=tuple(locus), args={}))
			continue
		if statement.startswith("measure"):
			match = re.match(r"^measure\s+(.+)\s+->\s+(.+)$", statement)
			if not match:
				raise RuntimeError(
					f"unsupported OpenQASM measurement: {statement}")
			qrefs = parse_ref(match.group(1), qregs)
			cref = match.group(2).strip()
			key = "m"
			match_cref = re.match(r"^(\w+)\[(\d+)\]$", cref)
			if match_cref:
				key = f"{match_cref.group(1)}{match_cref.group(2)}"
			elif cref not in cregs:
				raise RuntimeError(
					f"unknown OpenQASM classical reference {cref}")
			operations.append(CircuitOperation(
				name="measure",
				locus=tuple(qubit_map[key] for key in qrefs),
				args={"key": key},
			))
			continue
		match = re.match(r"^(\w+)(?:\(([^)]*)\))?\s+(.+)$", statement)
		if not match:
			raise RuntimeError(
				f"unsupported OpenQASM statement: {statement}")
		gate, params, refs = match.group(1), match.group(2), match.group(3)
		refs = [ref.strip() for ref in refs.split(",")]
		locus = tuple(
			qubit_map[key]
			for ref in refs
			for key in parse_ref(ref, qregs)
		)
		if gate == "x":
			operations.append(CircuitOperation(
				name="prx", locus=locus,
				args={"angle": math.pi, "phase": 0.0}))
		elif gate == "rx" and params is not None:
			operations.append(CircuitOperation(
				name="prx", locus=locus,
				args={"angle": eval_angle(params), "phase": 0.0}))
		elif gate == "ry" and params is not None:
			operations.append(CircuitOperation(
				name="prx", locus=locus,
				args={"angle": eval_angle(params), "phase": math.pi / 2}))
		elif gate == "cz":
			operations.append(CircuitOperation(
				name="cz", locus=locus, args={}))
		else:
			raise RuntimeError(
				"IQM direct backend can only translate native OpenQASM "
				"gates x, rx, ry, cz, barrier, measure. Unsupported "
				f"statement: {statement}")

	return Circuit(
		name="qhw_iqm_circuit",
		instructions=tuple(operations),
		metadata={"logical_to_physical": to_jsonable(qubit_map)},
	)


def normalize_counts(measurement_counts):
	data = to_jsonable(measurement_counts)
	if isinstance(data, list) and data:
		first = data[0]
	elif isinstance(data, dict):
		first = data
	else:
		return {}
	if isinstance(first, dict) and "counts" in first:
		return first.get("counts") or {}
	return first if isinstance(first, dict) else {}


def normalize_counts_list(measurement_counts):
	data = to_jsonable(measurement_counts)
	if isinstance(data, list):
		return [normalize_counts(item) for item in data]
	return [normalize_counts(data)]


class DirectIQMBackend:
	name = "direct"

	def __init__(self):
		self._client = None
		self._qiskit_provider = None
		self._qiskit_backends = {}
		self._config = load_env()
		self._qhw_device_id = qhw_device_id()
		self._request_timeout = get_env_float(
			"QHW_IQM_REQUEST_TIMEOUT", DEFAULT_REQUEST_TIMEOUT)
		self._job_timeout = get_env_float(
			"QHW_IQM_JOB_TIMEOUT", DEFAULT_JOB_TIMEOUT)

	def _qhw_tags(self, kind: str) -> dict[str, str | None]:
		return {
			QHW_IQM_KIND_KEY: kind,
			QHW_IQM_DEVICE_ID_KEY: self._qhw_device_id,
		}

	def _normalize_qhw(self, kind: str,
			   raw_payload: dict[str, Any]) -> dict[str, Any]:
		return normalize_iqm_payload(
			kind, raw_payload, device_id=self._qhw_device_id)

	def client(self):
		if self._client is None:
			client_type = load_iqm_client_module()
			self._client = create_iqm_client(client_type, self._config)
		return self._client

	def get_static_architecture(self):
		return call_iqm_method(
			self.client().get_static_quantum_architecture,
			self._request_timeout)

	def get_dynamic_architecture(self, calibration_set_id=None):
		calibration_set_id = parse_calibration_set_id(calibration_set_id)
		return call_iqm_method(
			self.client().get_dynamic_quantum_architecture,
			self._request_timeout,
			calibration_set_id)

	def get_backend_info(self):
		static = to_jsonable(self.get_static_architecture())
		dynamic = to_jsonable(self.get_dynamic_architecture())
		raw_payload = {
			"static_architecture": static,
			"dynamic_architecture": dynamic,
		}
		return {
			"backend": "iqm-direct",
			"metadata_supported": True,
			"endpoint": {
				"url": sanitize_url(self._config.url),
				"quantum_computer": self._config.quantum_computer,
			},
			"static_architecture": static,
			"active_qubits": get_dynamic_qubits(dynamic),
			"calibration_set_id": dynamic.get("calibration_set_id"),
			"qhw_device": self._normalize_qhw("device", raw_payload),
			**self._qhw_tags("device"),
			"_raw_iqm": raw_payload,
		}

	def get_device_info(self):
		static = to_jsonable(self.get_static_architecture())
		dynamic = to_jsonable(self.get_dynamic_architecture())
		raw_payload = {
			"static_architecture": static,
			"dynamic_architecture": dynamic,
		}
		return self._normalize_qhw("device", raw_payload)

	def get_dynamic_backend_info(self, calibration_set_id=None):
		dynamic = to_jsonable(
			self.get_dynamic_architecture(calibration_set_id))
		return {
			"backend": "iqm-direct",
			"metadata_supported": True,
			"dynamic_architecture": dynamic,
			"_raw_iqm": {
				"dynamic_architecture": dynamic,
			},
		}

	def get_calibration_snapshot(self, calibration_set_id=None):
		requested_calibration_set_id = parse_calibration_set_id(
			calibration_set_id)
		dynamic = to_jsonable(
			self.get_dynamic_architecture(requested_calibration_set_id))
		calibration = fetch_iqm_section(
			"calibration_set",
			lambda: call_iqm_method(
				self.client().get_calibration_set,
				self._request_timeout,
				requested_calibration_set_id))
		quality = fetch_iqm_section(
			"quality_metric_set",
			lambda: call_iqm_method(
				self.client().get_quality_metric_set,
				self._request_timeout,
				requested_calibration_set_id))
		errors = {
			result["name"]: result["error"]
			for result in (calibration, quality)
			if not result["ok"]
		}
		raw_payload = {
			"dynamic_architecture": dynamic,
			"calibration_set": calibration["data"],
			"quality_metric_set": quality["data"],
			"errors": errors,
		}
		return self._normalize_qhw("calibration", raw_payload)

	def get_coupling_graph(self, calibration_set_id=None):
		static = to_jsonable(self.get_static_architecture())
		dynamic = to_jsonable(
			self.get_dynamic_architecture(calibration_set_id))
		raw_payload = {
			"static_architecture": static,
			"dynamic_architecture": dynamic,
		}
		return self._normalize_qhw("coupling", raw_payload)

	def _run_iqm_circuits(self, infos):
		if not infos:
			raise RuntimeError("no circuits were supplied")
		first = infos[0]
		calibration_set_id = parse_calibration_set_id(
			first.get("calibration_set_id")
			or first.get("iqm_calibration_set_id"))
		use_timeslot = bool(first.get("use_timeslot", False))
		shots = int(first.get("num_shots", first.get("shots", 1)))
		timeout = float(first.get("timeout", self._job_timeout))
		cid = first.get("cid") or f"direct-{int(time.time() * 1000)}"

		for info in infos[1:]:
			info_calibration = parse_calibration_set_id(
				info.get("calibration_set_id")
				or info.get("iqm_calibration_set_id"))
			info_shots = int(info.get("num_shots", info.get("shots", 1)))
			if info_calibration != calibration_set_id:
				raise RuntimeError(
					"direct IQM batch submission requires all circuits "
					"to use the same calibration set")
			if info_shots != shots:
				raise RuntimeError(
					"direct IQM batch submission requires all circuits "
					"to use the same shot count")

		timing = {}
		start = time.monotonic()
		dynamic = self.get_dynamic_architecture(calibration_set_id)
		iqm_circuits = []
		for info in infos:
			mapping = (
				info.get("iqm_qubit_mapping") or info.get("qubit_mapping"))
			iqm_circuits.append(build_manual_iqm_circuit(
				info["qasm"], dynamic, mapping))
		run_request = self.client().create_run_request(
			iqm_circuits,
			calibration_set_id=calibration_set_id,
			shots=shots)
		run_request_data = to_jsonable(run_request)
		circuit_data = to_jsonable(iqm_circuits)

		submit_started = time.monotonic()
		job = submit_run_request(self.client(), run_request, use_timeslot)
		if not hasattr(job, "wait_for_completion"):
			raise RuntimeError(
				"iqm-client returned only a job id from submit_run_request. "
				"Direct mode requires CircuitJob polling support.")
		timing["submit_seconds"] = time.monotonic() - submit_started

		wait_started = time.monotonic()
		if method_accepts(job.wait_for_completion, "timeout_secs"):
			status = normalize_status(
				job.wait_for_completion(timeout_secs=timeout))
		else:
			status = normalize_status(job.wait_for_completion())
		timing["wait_seconds"] = time.monotonic() - wait_started
		job_data = to_jsonable(job.data)

		if status != "completed":
			raise RuntimeError(
				f"IQM job {job.job_id} completed with status {status}")

		result_started = time.monotonic()
		measurement_counts = self.client().get_job_measurement_counts(
			job.job_id)
		timing["result_fetch_seconds"] = time.monotonic() - result_started
		timing["total_wall_seconds"] = time.monotonic() - start

		counts_data = to_jsonable(measurement_counts)
		counts = normalize_counts(measurement_counts)
		counts_by_circuit = normalize_counts_list(measurement_counts)
		record = {
			"cid": cid,
			"timestamp_utc": datetime.now(timezone.utc).isoformat(),
			"input": {
				"num_circuits": len(infos),
				"shots": shots,
				"calibration_set_id": str(calibration_set_id)
				if calibration_set_id else None,
				"use_timeslot": use_timeslot,
			},
			"job": {
				"id": str(job.job_id),
				"status": status,
				"data": job_data,
			},
			"timing": timing,
			"results": {
				"measurement_counts": counts_data,
			},
		}
		raw_payload = {
			"circuits": circuit_data,
			"run_request": run_request_data,
			"job": job_data,
			"measurement_counts": counts_data,
		}
		qhw_result = self._normalize_qhw("result", raw_payload)
		return {
			"cid": cid,
			"result": {
				"counts": counts if len(infos) == 1 else counts_by_circuit,
				"qhw_result": qhw_result,
				"iqm": {
					"job_id": str(job.job_id),
					"status": status,
					"batch_semantics": "single-iqm-job",
					"num_circuits": len(infos),
					"counts_by_circuit": counts_by_circuit,
					"measurement_counts": counts_data,
					"metadata": record,
				},
			},
			**self._qhw_tags("result"),
			"_raw_iqm": raw_payload,
			"rc": 0,
		}

	def sync_run(self, info):
		return self._run_iqm_circuits([info])

	def sync_run_many(self, infos):
		return self._run_iqm_circuits(infos)

	def qiskit_provider(self):
		if self._qiskit_provider is None:
			try:
				from iqm.qiskit_iqm import IQMProvider
			except Exception as exc:
				raise RuntimeError(
					"iqm.qiskit_iqm is required for direct Qiskit "
					f"execution: {exc}") from exc
			kwargs = {"token": self._config.api_key}
			if self._config.quantum_computer:
				kwargs["quantum_computer"] = self._config.quantum_computer
			self._qiskit_provider = IQMProvider(self._config.url, **kwargs)
		return self._qiskit_provider

	def qiskit_backend(self, calibration_set_id=None):
		calibration_set_id = parse_calibration_set_id(calibration_set_id)
		cache_key = str(calibration_set_id) if calibration_set_id else "default"
		if cache_key not in self._qiskit_backends:
			self._qiskit_backends[cache_key] = (
				self.qiskit_provider().get_backend(
					calibration_set_id=calibration_set_id))
		return self._qiskit_backends[cache_key]

	def qiskit_run_options(self, shots: int, calibration_set_id=None,
	    timeout=None, use_timeslot=False, qubit_mapping=None,
	    extra_run_options=None):
		del calibration_set_id, timeout
		options = {
			"shots": shots,
			"use_timeslot": use_timeslot,
		}
		if qubit_mapping:
			options["qubit_mapping"] = qubit_mapping
		options.update(extra_run_options or {})
		return options

	def qiskit_job_result(self, job, timeout=None):
		return job.result(timeout=timeout or self._job_timeout)

	def qiskit_record_extra(self, context):
		del context
		return None

	def extract_result_and_normalize(self, job, result, record, context):
		del result, context
		iqm_job = optional_attr_data(job, "_job")
		raw_payload = {
			"qiskit_result": (
				record.get("result", {}).get("qiskit", {}).get("result")),
			"iqm_job": iqm_job,
		}
		qhw_result = self._normalize_qhw("result", raw_payload)
		record.setdefault("result", {})["qhw_result"] = qhw_result
		self._apply_qhw_timing(record, qhw_result)
		record.update(self._qhw_tags("result"))
		record["_raw_iqm"] = raw_payload
		return record

	def _apply_qhw_timing(self, record, qhw_result):
		timing = qhw_result.get("timing")
		if not isinstance(timing, dict) or not timing:
			return
		timing_summary = record.setdefault("result", {}).setdefault(
			"timing_summary", {})
		timing_summary["backend_timing"] = timing
		timing_summary["durations_seconds"] = timing.get(
			"durations_seconds", {})
		timing_summary["timeline_events"] = (
			timing.get("timeline")
			or timing.get("timeline_events")
			or [])

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
		return rc
