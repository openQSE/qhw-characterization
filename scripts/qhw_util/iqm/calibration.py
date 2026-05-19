"""IQM calibration analysis helpers."""

from __future__ import annotations

from statistics import mean, median
from typing import Any, Callable

from qhw_util.schema import qhw_calibration_set_id
from qhw_util.schema import qhw_quality_metric_set_id


FieldMatcher = Callable[[str], bool]


def _ends_with_metric(field: str, prefix: str, suffix: str) -> bool:
	return field.startswith(prefix) and field.endswith(suffix)


def _contains_metric(field: str, prefix: str, token: str) -> bool:
	return field.startswith(prefix) and token in field


_IQM_QUALITY_METRICS: tuple[dict[str, Any], ...] = (
	{
		"name": "t1_time",
		"label": "T1 time",
		"field_pattern": "characterization.model.QB*.t1_time",
		"match": lambda field: _ends_with_metric(
			field, "characterization.model.", ".t1_time"),
		"display_unit": "us",
		"display_scale": 1e6,
	},
	{
		"name": "t2_time_ramsey",
		"label": "T2 time (Ramsey)",
		"field_pattern": "characterization.model.QB*.t2_time",
		"match": lambda field: _ends_with_metric(
			field, "characterization.model.", ".t2_time"),
		"display_unit": "us",
		"display_scale": 1e6,
	},
	{
		"name": "t2_time_echo",
		"label": "T2 time (echo)",
		"field_pattern": "characterization.model.QB*.t2_echo_time",
		"match": lambda field: _ends_with_metric(
			field, "characterization.model.", ".t2_echo_time"),
		"display_unit": "us",
		"display_scale": 1e6,
	},
	{
		"name": "prx_gate_fidelity",
		"label": "PRX gate fidelity",
		"field_pattern": "metrics.rb.prx.drag_crf_sx.QB*.fidelity:par=d2",
		"match": lambda field: _contains_metric(
			field, "metrics.rb.prx.drag_crf_sx.", ".fidelity"),
		"display_unit": "%",
		"display_scale": 100,
	},
	{
		"name": "single_qubit_readout_fidelity",
		"label": "Single-qubit readout fidelity",
		"field_pattern": "metrics.ssro.measure_fidelity.constant.QB*.fidelity",
		"match": lambda field: _ends_with_metric(
			field, "metrics.ssro.measure_fidelity.constant.", ".fidelity"),
		"display_unit": "%",
		"display_scale": 100,
	},
	{
		"name": "cz_gate_fidelity",
		"label": "CZ gate fidelity",
		"field_pattern": "metrics.irb.cz.crf_crf.QB*__QB*.fidelity:par=d2",
		"match": lambda field: _contains_metric(
			field, "metrics.irb.cz.crf_crf.", ".fidelity"),
		"display_unit": "%",
		"display_scale": 100,
	},
	{
		"name": "cliffords_averaged_gate_fidelity",
		"label": "Cliffords averaged gate fidelity",
		"field_pattern": "metrics.rb.clifford.uz_cz.QB*__QB*.fidelity:par=d2",
		"match": lambda field: _contains_metric(
			field, "metrics.rb.clifford.uz_cz.", ".fidelity"),
		"display_unit": "%",
		"display_scale": 100,
	},
)


def _iqm_quality_observations(
		calibration_info: dict[str, Any]) -> list[dict[str, Any]]:
	extensions = calibration_info.get("extensions") or {}
	iqm = extensions.get("iqm.v1") or {}
	quality_metric_set = iqm.get("quality_metric_set") or {}
	observations = quality_metric_set.get("observations") or []
	return [
		observation for observation in observations
		if isinstance(observation, dict)
	]


def _numeric_observation_value(observation: dict[str, Any]) -> float | None:
	if observation.get("invalid"):
		return None
	value = observation.get("value")
	if isinstance(value, bool) or not isinstance(value, (int, float)):
		return None
	return float(value)


def _metric_summary(
		definition: dict[str, Any],
		observations: list[dict[str, Any]]) -> dict[str, Any] | None:
	matcher: FieldMatcher = definition["match"]
	values = []
	units = set()
	for observation in observations:
		field = str(observation.get("dut_field") or "")
		if not matcher(field):
			continue
		value = _numeric_observation_value(observation)
		if value is None:
			continue
		values.append(value)
		if observation.get("unit") is not None:
			units.add(str(observation.get("unit")))

	if not values:
		return None

	scale = float(definition["display_scale"])
	return {
		"label": definition["label"],
		"field_pattern": definition["field_pattern"],
		"count": len(values),
		"unit": sorted(units)[0] if len(units) == 1 else None,
		"average": mean(values),
		"median": median(values),
		"display": {
			"unit": definition["display_unit"],
			"average": mean(values) * scale,
			"median": median(values) * scale,
		},
	}


def qhw_iqm_calibration_quality_summary(
		calibration_info: dict[str, Any]) -> dict[str, Any]:
	"""Summarize IQM dashboard-style quality metrics."""
	observations = _iqm_quality_observations(calibration_info)
	metrics = {}
	for definition in _IQM_QUALITY_METRICS:
		summary = _metric_summary(definition, observations)
		if summary is not None:
			metrics[definition["name"]] = summary

	return {
		"schema": "qhw-calibration-quality-summary-v1",
		"provider": calibration_info.get("provider"),
		"calibration_set_id": qhw_calibration_set_id(calibration_info),
		"quality_metric_set_id": qhw_quality_metric_set_id(
			calibration_info),
		"metrics": metrics,
	}
