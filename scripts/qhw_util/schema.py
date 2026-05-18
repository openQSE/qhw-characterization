"""Helpers for reading qhw-data schema records."""

from __future__ import annotations

from typing import Any


def qhw_device_qubits(device_info: dict[str, Any]) -> list[str]:
	"""Return qubit ids from a qhw-device-v1 record."""
	qubits = device_info.get("qubits") or []
	result = []
	for qubit in qubits:
		if isinstance(qubit, dict):
			qubit_id = qubit.get("id")
		else:
			qubit_id = qubit
		if qubit_id is not None:
			result.append(str(qubit_id))
	return result


def qhw_device_name(device_info: dict[str, Any]) -> str | None:
	"""Return the most useful display name from a qhw-device-v1 record."""
	device = device_info.get("device") or {}
	return (
		device.get("name")
		or device.get("provider_device_id")
		or device.get("id")
	)


def qhw_device_summary(device_info: dict[str, Any]) -> dict[str, Any]:
	"""Build a compact device summary from a qhw-device-v1 record."""
	device = device_info.get("device") or {}
	qubits = qhw_device_qubits(device_info)
	return {
		"schema": device_info.get("schema"),
		"provider": device_info.get("provider") or device.get("provider"),
		"id": device.get("id"),
		"name": qhw_device_name(device_info),
		"technology": device.get("technology"),
		"num_qubits": device.get("num_qubits") or len(qubits),
		"active_qubits": qubits,
		"calibration_set_id": (
			(device_info.get("metadata") or {}).get("calibration_set_id")
		),
	}


def qhw_coupling_nodes(coupling_info: dict[str, Any]) -> list[str]:
	"""Return graph nodes from a qhw-coupling-v1 record."""
	coupling = coupling_info.get("coupling") or {}
	return [str(node) for node in coupling.get("nodes") or []]


def qhw_coupling_edges(coupling_info: dict[str, Any]) -> list[list[str]]:
	"""Return graph edges from a qhw-coupling-v1 record."""
	coupling = coupling_info.get("coupling") or {}
	edges = []
	for edge in coupling.get("edges") or []:
		if isinstance(edge, (list, tuple)):
			edges.append([str(node) for node in edge])
	return edges


def qhw_calibration_set_id(calibration_info: dict[str, Any]) -> str | None:
	"""Return the calibration set id from a qhw-calibration-v1 record."""
	calibration = calibration_info.get("calibration") or {}
	value = calibration.get("calibration_set_id")
	return str(value) if value is not None else None


def qhw_quality_metric_set_id(calibration_info: dict[str, Any]) -> str | None:
	"""Return the quality metric set id from a qhw-calibration-v1 record."""
	calibration = calibration_info.get("calibration") or {}
	value = calibration.get("quality_metric_set_id")
	return str(value) if value is not None else None
