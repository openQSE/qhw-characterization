"""Shared helpers for qhw characterization experiments."""

from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path
from typing import Any

from qhw_util.output import to_jsonable


def parse_int_list(value: str) -> list[int]:
	items = []
	for raw in value.split(","):
		raw = raw.strip()
		if not raw:
			continue
		item = int(raw)
		if item < 1:
			raise argparse.ArgumentTypeError(
				f"list values must be positive integers: {value!r}")
		items.append(item)
	if not items:
		raise argparse.ArgumentTypeError("list must contain at least one value")
	return items


def parse_float_list(value: str) -> list[float]:
	items = []
	for raw in value.split(","):
		raw = raw.strip()
		if not raw:
			continue
		item = float(raw)
		if item < 0:
			raise argparse.ArgumentTypeError(
				f"list values must be non-negative: {value!r}")
		items.append(item)
	if not items:
		raise argparse.ArgumentTypeError("list must contain at least one value")
	return items


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	text = "\n".join(
		json.dumps(to_jsonable(record), sort_keys=True)
		for record in records)
	if text:
		text += "\n"
	path.write_text(text)


def dry_run_result(cid: str, shots: int, counts: dict[str, int] | None = None,
		   execution_seconds: float | None = None) -> dict[str, Any]:
	durations = {}
	if execution_seconds is not None:
		durations = {
			"execution_seconds": execution_seconds,
			"provider_total_seconds": execution_seconds,
		}
	return {
		"cid": cid,
		"result": {
			"qhw_result": {
				"schema": "qhw-result-v1",
				"provider": "dry-run",
				"device": {"id": "dry-run", "provider": "dry-run"},
				"job": {"id": cid, "status": "completed"},
				"result": {
					"shots": shots,
					"num_circuits": 1,
					"counts": counts or {},
					"success": True,
				},
				"timing": {
					"timestamps": {},
					"timeline": [],
					"durations_seconds": durations,
				},
				"errors": [],
				"extensions": {},
				"raw": {"included": False, "format": None, "artifacts": []},
			},
		},
		"rc": 0,
	}


def active_qubits_for_dry_run(count: int = 20) -> list[str]:
	return [f"QB{index}" for index in range(1, count + 1)]


def resolve_qubits(value: str, active_qubits: list[Any],
		   dry_run: bool) -> list[str]:
	if value.strip().lower() == "all":
		if active_qubits:
			return [str(qubit) for qubit in active_qubits]
		if dry_run:
			return active_qubits_for_dry_run()
		raise ValueError("qubits=all requires backend device metadata")
	qubits = [item.strip() for item in value.split(",") if item.strip()]
	if not qubits:
		raise ValueError("at least one qubit must be selected")
	return qubits


def resolve_widths(value: str, qubits: list[str]) -> list[int]:
	widths = []
	max_width = len(qubits)
	for raw in value.split(","):
		raw = raw.strip().lower()
		if not raw:
			continue
		if raw == "all":
			widths.extend(range(1, max_width + 1))
		elif raw == "max":
			widths.append(max_width)
		else:
			width = int(raw)
			if width < 1:
				raise ValueError(f"width must be positive: {raw!r}")
			if width > max_width:
				raise ValueError(
					f"width {width} exceeds selected qubit count {max_width}")
			widths.append(width)
	if not widths:
		raise ValueError("at least one width must be selected")
	return sorted(set(widths))


def logical_bits_to_count_key(bits: list[int] | list[str] | str) -> str:
	if isinstance(bits, str):
		values = list(bits)
	else:
		values = [str(bit) for bit in bits]
	return "".join(reversed(values))


def normalize_count_key(key: Any, width: int) -> str:
	text = str(key).replace(" ", "")
	if text.startswith("0x"):
		return format(int(text, 16), f"0{width}b")[-width:]
	if set(text) <= {"0", "1"}:
		return text.zfill(width)[-width:]
	return text


def bit_for_logical_index(count_key: Any, width: int,
			  logical_index: int) -> int:
	key = normalize_count_key(count_key, width)
	return int(key[width - 1 - logical_index])


def counts_total(counts: dict[str, Any]) -> int:
	return sum(int(value) for value in counts.values())


def logical_one_probability(counts: dict[str, Any], width: int,
			    logical_index: int) -> float | None:
	total = counts_total(counts)
	if total == 0:
		return None
	ones = 0
	for key, value in counts.items():
		if bit_for_logical_index(key, width, logical_index):
			ones += int(value)
	return ones / total


def success_probability(counts: dict[str, Any], width: int,
			expected_bits: list[int] | str) -> float | None:
	total = counts_total(counts)
	if total == 0:
		return None
	expected_key = logical_bits_to_count_key(expected_bits)
	success = 0
	for key, value in counts.items():
		if normalize_count_key(key, width) == expected_key:
			success += int(value)
	return success / total


def hamming_error_probability(counts: dict[str, Any], width: int,
			      expected_bits: list[int] | str) -> float | None:
	total = counts_total(counts)
	if total == 0:
		return None
	if isinstance(expected_bits, str):
		expected = [int(bit) for bit in expected_bits]
	else:
		expected = [int(bit) for bit in expected_bits]
	errors = 0
	for key, value in counts.items():
		for index, bit in enumerate(expected):
			if bit_for_logical_index(key, width, index) != bit:
				errors += int(value)
	return errors / (total * width)


def independent_readout_counts(expected_bits: list[int] | str, shots: int,
			       error: float = 0.02,
			       correlation: float = 0.0) -> dict[str, int]:
	if isinstance(expected_bits, str):
		expected = [int(bit) for bit in expected_bits]
	else:
		expected = [int(bit) for bit in expected_bits]
	width = len(expected)
	probabilities: dict[str, float] = {}
	for mask in range(1 << width):
		observed = []
		probability = 1.0
		for index, bit in enumerate(expected):
			flip = (mask >> index) & 1
			probability *= error if flip else 1.0 - error
			observed.append(bit ^ flip)
		key = logical_bits_to_count_key(observed)
		probabilities[key] = probabilities.get(key, 0.0) + probability
	if width > 1 and correlation > 0:
		correlated = [1 - bit for bit in expected]
		key = logical_bits_to_count_key(correlated)
		base_key = logical_bits_to_count_key(expected)
		shift = min(correlation, probabilities.get(base_key, 0.0))
		probabilities[base_key] -= shift
		probabilities[key] = probabilities.get(key, 0.0) + shift
	counts = {
		key: int(round(probability * shots))
		for key, probability in probabilities.items()
	}
	delta = shots - sum(counts.values())
	if delta:
		best_key = max(counts, key=counts.get)
		counts[best_key] += delta
	return {key: value for key, value in counts.items() if value}


def mean_or_none(values: list[float | None]) -> float | None:
	items = [value for value in values if value is not None]
	return statistics.fmean(items) if items else None


def linear_fit(points: list[tuple[float, float]]) -> dict[str, Any] | None:
	if len(points) < 2:
		return None
	n = len(points)
	xs = [point[0] for point in points]
	ys = [point[1] for point in points]
	sx = sum(xs)
	sy = sum(ys)
	sxx = sum(x * x for x in xs)
	sxy = sum(x * y for x, y in points)
	denom = n * sxx - sx * sx
	if denom == 0:
		return None
	slope = (n * sxy - sx * sy) / denom
	intercept = (sy - slope * sx) / n
	residuals = [y - (intercept + slope * x) for x, y in points]
	return {
		"intercept": intercept,
		"slope": slope,
		"rms_residual": (
			sum(value * value for value in residuals) / n) ** 0.5,
		"points": n,
		"x_min": min(xs),
		"x_max": max(xs),
	}


def exponential_decay_fit(points: list[tuple[float, float]],
			  floor: float = 0.0) -> dict[str, Any] | None:
	transformed = []
	for x_value, y_value in points:
		adjusted = y_value - floor
		if adjusted <= 0:
			continue
		transformed.append((x_value, math.log(adjusted)))
	fit = linear_fit(transformed)
	if not fit or fit["slope"] >= 0:
		return None
	decay_constant = -1.0 / fit["slope"]
	return {
		"amplitude": math.exp(fit["intercept"]),
		"decay_constant": decay_constant,
		"floor": floor,
		"points": fit["points"],
		"rms_log_residual": fit["rms_residual"],
	}
