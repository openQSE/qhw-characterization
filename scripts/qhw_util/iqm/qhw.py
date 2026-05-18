"""Helpers for using qhw-iqm normalizers from qhw workflows."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import os

QHW_IQM_KIND_KEY = "_qhw_iqm_kind"
QHW_IQM_DEVICE_ID_KEY = "_qhw_iqm_device_id"


def qhw_device_id(default: str | None = None) -> str | None:
	value = (
		os.environ.get("QFW_QPU_DEVICE_ID")
		or os.environ.get("QHW_IQM_DEVICE_ID")
		or default)
	return value.strip() if isinstance(value, str) and value.strip() else None


def normalize_iqm_payload(kind: str,
			  raw_payload: dict[str, Any],
			  device_id: str | None = None) -> dict[str, Any]:
	try:
		from qhw_iqm import normalize_calibration, normalize_coupling
		from qhw_iqm import normalize_device, normalize_result
	except Exception as exc:
		raise RuntimeError(
			"qhw-iqm is required to normalize direct IQM payloads. "
			"Install workflow requirements with: "
			"python3 -m pip install -r requirements.txt") from exc

	resolved_device_id = qhw_device_id(device_id)
	if kind == "device":
		return normalize_device(raw_payload, device_id=resolved_device_id)
	if kind == "coupling":
		return normalize_coupling(raw_payload, device_id=resolved_device_id)
	if kind == "calibration":
		return normalize_calibration(raw_payload, device_id=resolved_device_id)
	if kind == "result":
		return normalize_result(raw_payload, device_id=resolved_device_id)
	raise ValueError(f"unsupported qhw-iqm normalization kind: {kind!r}")


def write_qhw_iqm_json(path: Path,
		       kind: str,
		       raw_payload: dict[str, Any],
		       device_id: str | None = None) -> None:
	normalized = normalize_iqm_payload(kind, raw_payload, device_id)
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(json.dumps(normalized, indent=2, sort_keys=True))
