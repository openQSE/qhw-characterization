"""Shared Qiskit execution helpers for qhw test workflows."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import time

from qhw_util.output import to_jsonable


def ensure_circuit_list(circuits):
	try:
		from qiskit import QuantumCircuit
	except Exception as exc:
		raise RuntimeError(
			"qiskit is required for qhw circuit workflows") from exc

	if isinstance(circuits, QuantumCircuit):
		return [circuits]
	return list(circuits)


def write_qasm2_artifact(circuit, path: Path) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	try:
		from qiskit import qasm2
		path.write_text(qasm2.dumps(circuit))
		return
	except Exception:
		pass
	if hasattr(circuit, "qasm"):
		path.write_text(circuit.qasm())
		return
	raise RuntimeError("failed to serialize Qiskit circuit as OpenQASM 2")


def get_job_id(job) -> str | None:
	if not job:
		return None
	try:
		job_id = job.job_id()
	except TypeError:
		job_id = getattr(job, "job_id", None)
	except Exception:
		job_id = None
	return str(job_id) if job_id else None


def result_to_dict(result) -> dict[str, Any]:
	if hasattr(result, "to_dict"):
		return to_jsonable(result.to_dict())
	return to_jsonable(result)


def get_counts(result, circuits: list[Any]) -> Any:
	counts = []
	for index, circuit in enumerate(circuits):
		try:
			counts.append(to_jsonable(result.get_counts(circuit)))
			continue
		except Exception:
			pass
		try:
			counts.append(to_jsonable(result.get_counts(index)))
			continue
		except Exception:
			pass
		counts.append({})
	return counts[0] if len(counts) == 1 else counts


def optional_attr_data(obj, attr_name: str) -> Any:
	try:
		value = getattr(obj, attr_name)
	except Exception:
		return None
	if callable(value):
		try:
			value = value()
		except Exception:
			return None
	return to_jsonable(value)


def _qfw_backend_metadata(result_dict: dict[str, Any]) -> list[dict[str, Any]]:
	metadata = []
	for experiment in result_dict.get("results", []) or []:
		if not isinstance(experiment, dict):
			continue
		data = experiment.get("data", {})
		if not isinstance(data, dict):
			continue
		item = data.get("metadata")
		if isinstance(item, dict) and item:
			metadata.append(item)
	return metadata


def qiskit_result_metadata(result_dict: dict[str, Any]) -> list[dict[str, Any]]:
	"""Return per-experiment metadata entries from a Qiskit result dict."""
	return _qfw_backend_metadata(result_dict)


def _metadata_qhw_timings(
		metadata: list[dict[str, Any]]) -> list[dict[str, Any]]:
	summaries = []
	for item in metadata:
		qhw_result = item.get("qhw_result")
		if not isinstance(qhw_result, dict):
			continue
		timing = qhw_result.get("timing")
		if isinstance(timing, dict) and timing:
			summaries.append(timing)
	return summaries


def _set_timing_summary_backend_timing(
		timing_summary: dict[str, Any],
		backend_timing: dict[str, Any]) -> None:
	timing_summary["backend_timing"] = backend_timing
	timing_summary["durations_seconds"] = backend_timing.get(
		"durations_seconds", {})
	timing_summary["timeline_events"] = (
		backend_timing.get("timeline")
		or backend_timing.get("timeline_events")
		or [])


def _add_backend_timing(timing_summary: dict[str, Any],
			result_dict: dict[str, Any],
			job_id: str | None,
			wall_seconds: float) -> None:
	del job_id, wall_seconds
	metadata = _qfw_backend_metadata(result_dict)
	backend_summaries = _metadata_qhw_timings(metadata)
	if not backend_summaries:
		return

	timing_summary["backend_timing_summaries"] = backend_summaries
	if len(backend_summaries) == 1:
		_set_timing_summary_backend_timing(
			timing_summary, backend_summaries[0])


def build_qiskit_run_record(backend_name: str,
			    circuits,
			    shots: int,
			    run_start: float,
			    job,
			    result,
			    extra: dict[str, Any] | None = None) -> dict[str, Any]:
	circuit_list = ensure_circuit_list(circuits)
	wall_seconds = time.monotonic() - run_start
	job_id = get_job_id(job)
	result_dict = result_to_dict(result)
	timing_summary = {
		"schema": "qhw-qiskit-timing-summary-v1",
		"job_id": job_id,
		"backend": backend_name,
		"timestamp_utc": datetime.now(timezone.utc).isoformat(),
		"num_circuits": len(circuit_list),
		"shots": shots,
		"client_wall_seconds": {
			"total": wall_seconds,
		},
	}
	_add_backend_timing(timing_summary, result_dict, job_id, wall_seconds)
	payload = {
		"cid": job_id,
		"result": {
			"counts": get_counts(result, circuit_list),
			"timing_summary": timing_summary,
			"qiskit": {
				"job_id": job_id,
				"result": result_dict,
			},
		},
		"rc": 0,
	}
	if extra:
		payload["result"].update(to_jsonable(extra))
	return payload
