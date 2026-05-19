"""Provider-neutral calibration analysis dispatch."""

from __future__ import annotations

from typing import Any

from qhw_util.schema import qhw_calibration_set_id
from qhw_util.schema import qhw_quality_metric_set_id


def _empty_calibration_quality_summary(
		calibration_info: dict[str, Any]) -> dict[str, Any]:
	return {
		"schema": "qhw-calibration-quality-summary-v1",
		"provider": calibration_info.get("provider"),
		"calibration_set_id": qhw_calibration_set_id(calibration_info),
		"quality_metric_set_id": qhw_quality_metric_set_id(
			calibration_info),
		"metrics": {},
	}


def qhw_calibration_quality_summary(
		calibration_info: dict[str, Any]) -> dict[str, Any]:
	"""Build a compact quality summary for a calibration snapshot.

	Provider-specific calibration layouts live behind this dispatcher. Unknown
	providers still get a valid empty summary so generic workflows do not need
	to special-case provider support.
	"""
	provider = str(calibration_info.get("provider") or "").lower()
	if provider == "iqm":
		from qhw_util.iqm.calibration import (
			qhw_iqm_calibration_quality_summary,
		)
		return qhw_iqm_calibration_quality_summary(calibration_info)
	return _empty_calibration_quality_summary(calibration_info)
