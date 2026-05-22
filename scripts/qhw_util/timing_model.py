"""Timing-model helpers shared by qhw timing workflows."""

from __future__ import annotations

import statistics
from typing import Any


ONE_Q_INTERLEAVE_SEQUENCE = ("rx", "ry")


def sequence_key(sequence: list[str] | tuple[str, ...]) -> str:
	"""Return a stable key for a gate sequence."""
	return "_".join(str(gate) for gate in sequence)


def safe_float(value: Any) -> float | None:
	if value is None:
		return None
	try:
		return float(value)
	except (TypeError, ValueError):
		return None


def execution_per_shot(metrics: dict[str, Any]) -> float | None:
	"""Return the hardware execution-time metric used by timing scripts."""
	return safe_float(metrics.get("execution_per_shot_seconds"))


def mean_or_none(values: list[float | None]) -> float | None:
	items = [value for value in values if value is not None]
	return statistics.fmean(items) if items else None


def one_q_baseline_table(
		records: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
	"""Build qubit -> gate -> execution-per-shot baseline table."""
	values: dict[str, dict[str, list[float]]] = {}
	for record in records:
		if not record.get("ok"):
			continue
		value = execution_per_shot(record.get("metrics", {}))
		if value is None:
			continue
		qubit = str(record["physical_qubit"])
		gate = str(record["gate"])
		values.setdefault(qubit, {}).setdefault(gate, []).append(value)
	return {
		qubit: {
			gate: statistics.fmean(samples)
			for gate, samples in gates.items()
		}
		for qubit, gates in values.items()
	}


def two_q_baseline_table(records: list[dict[str, Any]]) -> dict[str, float]:
	"""Build gate:pair -> execution-per-shot baseline table."""
	values: dict[str, list[float]] = {}
	for record in records:
		if not record.get("ok"):
			continue
		value = execution_per_shot(record.get("metrics", {}))
		if value is None:
			continue
		key = str(record["baseline_key"])
		values.setdefault(key, []).append(value)
	return {
		key: statistics.fmean(samples)
		for key, samples in values.items()
	}


def one_q_sequence_model(
		baselines: dict[str, dict[str, float]],
		qubit: str,
		sequence: list[str],
		repetitions: int,
		observed_per_shot: float | None) -> dict[str, Any]:
	"""Model a 1Q sequence from single-gate baselines."""
	components = [
		baselines.get(str(qubit), {}).get(gate)
		for gate in sequence
	]
	sequence_seconds = None
	if all(value is not None for value in components):
		sequence_seconds = sum(value for value in components if value is not None)
	expected = (
		sequence_seconds * repetitions
		if sequence_seconds is not None else None)
	return {
		"model": "single_qubit_sequence_sum",
		"baseline_metric": "execution_per_shot_seconds",
		"sequence": list(sequence),
		"sequence_repetitions": repetitions,
		"sequence_gate_count": repetitions * len(sequence),
		"baseline_components_seconds": {
			gate: baselines.get(str(qubit), {}).get(gate)
			for gate in sequence
		},
		"expected_per_shot_seconds": expected,
		"observed_minus_expected_per_shot_seconds": (
			observed_per_shot - expected
			if observed_per_shot is not None and expected is not None else None),
	}


def parallel_one_q_sequence_model(
		baselines: dict[str, dict[str, float]],
		qubits: list[str],
		sequence: list[str],
		repetitions: int,
		observed_per_shot: float | None) -> dict[str, Any]:
	"""Model simultaneous 1Q layers using serial and ideal-parallel bounds."""
	serial_layer_seconds = 0.0
	parallel_layer_seconds = 0.0
	complete = True
	components: dict[str, dict[str, float | None]] = {}
	for gate in sequence:
		gate_values = {
			qubit: baselines.get(str(qubit), {}).get(gate)
			for qubit in qubits
		}
		components[gate] = gate_values
		values = list(gate_values.values())
		if any(value is None for value in values):
			complete = False
			continue
		serial_layer_seconds += sum(value for value in values if value is not None)
		parallel_layer_seconds += max(value for value in values if value is not None)

	serial_expected = serial_layer_seconds * repetitions if complete else None
	parallel_expected = parallel_layer_seconds * repetitions if complete else None
	return {
		"model": "one_qubit_layer_serial_vs_ideal_parallel",
		"baseline_metric": "execution_per_shot_seconds",
		"sequence": list(sequence),
		"sequence_repetitions": repetitions,
		"sequence_gate_count_per_qubit": repetitions * len(sequence),
		"baseline_components_seconds": components,
		"serial_expected_per_shot_seconds": serial_expected,
		"parallel_expected_per_shot_seconds": parallel_expected,
		"observed_minus_serial_per_shot_seconds": (
			observed_per_shot - serial_expected
			if observed_per_shot is not None and serial_expected is not None
			else None),
		"observed_minus_parallel_per_shot_seconds": (
			observed_per_shot - parallel_expected
			if observed_per_shot is not None and parallel_expected is not None
			else None),
	}


def two_q_baseline_key(gate: str, pair: tuple[str, str] | list[str]) -> str:
	return f"{gate}:{pair[0]}-{pair[1]}"


def two_q_sequence_model(
		one_q_baselines: dict[str, dict[str, float]],
		two_q_baselines: dict[str, float],
		gate: str,
		pair: tuple[str, str] | list[str],
		repetitions: int,
		observed_per_shot: float | None) -> dict[str, Any]:
	"""Model a 2Q sequence with pre/post 1Q interleaves."""
	left, right = str(pair[0]), str(pair[1])
	rx_values = [
		one_q_baselines.get(left, {}).get("rx"),
		one_q_baselines.get(right, {}).get("rx"),
	]
	ry_values = [
		one_q_baselines.get(left, {}).get("ry"),
		one_q_baselines.get(right, {}).get("ry"),
	]
	two_q_value = two_q_baselines.get(two_q_baseline_key(gate, pair))
	complete = (
		two_q_value is not None
		and all(value is not None for value in rx_values + ry_values))
	serial_expected = None
	parallel_expected = None
	if complete:
		serial_expected = repetitions * (
			sum(rx_values) + two_q_value + sum(ry_values))
		parallel_expected = repetitions * (
			max(rx_values) + two_q_value + max(ry_values))
	return {
		"model": "two_qubit_gate_with_1q_interleaves",
		"baseline_metric": "execution_per_shot_seconds",
		"sequence": ["rx_layer", gate, "ry_layer"],
		"sequence_repetitions": repetitions,
		"two_qubit_gate_count": repetitions,
		"single_qubit_gate_count": repetitions * 4,
		"baseline_components_seconds": {
			"rx": {left: rx_values[0], right: rx_values[1]},
			gate: {two_q_baseline_key(gate, pair): two_q_value},
			"ry": {left: ry_values[0], right: ry_values[1]},
		},
		"serial_expected_per_shot_seconds": serial_expected,
		"parallel_expected_per_shot_seconds": parallel_expected,
		"observed_minus_serial_per_shot_seconds": (
			observed_per_shot - serial_expected
			if observed_per_shot is not None and serial_expected is not None
			else None),
		"observed_minus_parallel_per_shot_seconds": (
			observed_per_shot - parallel_expected
			if observed_per_shot is not None and parallel_expected is not None
			else None),
	}


def parallel_two_q_sequence_model(
		one_q_baselines: dict[str, dict[str, float]],
		two_q_baselines: dict[str, float],
		gate: str,
		matching: list[list[str]],
		repetitions: int,
		observed_per_shot: float | None) -> dict[str, Any]:
	"""Model disjoint 2Q layers using serial and ideal-parallel bounds."""
	qubits = [str(qubit) for edge in matching for qubit in edge]
	rx_values = [one_q_baselines.get(qubit, {}).get("rx") for qubit in qubits]
	ry_values = [one_q_baselines.get(qubit, {}).get("ry") for qubit in qubits]
	two_q_values = [
		two_q_baselines.get(two_q_baseline_key(gate, edge))
		for edge in matching
	]
	complete = all(
		value is not None
		for value in rx_values + ry_values + two_q_values)
	serial_expected = None
	parallel_expected = None
	if complete:
		serial_expected = repetitions * (
			sum(rx_values) + sum(two_q_values) + sum(ry_values))
		parallel_expected = repetitions * (
			max(rx_values) + max(two_q_values) + max(ry_values))
	return {
		"model": "parallel_two_qubit_layer_serial_vs_ideal_parallel",
		"baseline_metric": "execution_per_shot_seconds",
		"sequence": ["rx_layer", gate, "ry_layer"],
		"sequence_repetitions": repetitions,
		"two_qubit_gate_count": repetitions * len(matching),
		"single_qubit_gate_count": repetitions * 2 * len(qubits),
		"baseline_components_seconds": {
			"rx": dict(zip(qubits, rx_values)),
			gate: {
				two_q_baseline_key(gate, edge): value
				for edge, value in zip(matching, two_q_values)
			},
			"ry": dict(zip(qubits, ry_values)),
		},
		"serial_expected_per_shot_seconds": serial_expected,
		"parallel_expected_per_shot_seconds": parallel_expected,
		"observed_minus_serial_per_shot_seconds": (
			observed_per_shot - serial_expected
			if observed_per_shot is not None and serial_expected is not None
			else None),
		"observed_minus_parallel_per_shot_seconds": (
			observed_per_shot - parallel_expected
			if observed_per_shot is not None and parallel_expected is not None
			else None),
	}


def expected_model_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
	"""Summarize observed-minus-expected residuals from record models."""
	values: dict[str, list[float]] = {}
	for record in records:
		if not record.get("ok"):
			continue
		expected = record.get("expected")
		if not isinstance(expected, dict):
			continue
		for key, value in expected.items():
			if not key.startswith("observed_minus_"):
				continue
			item = safe_float(value)
			if item is not None:
				values.setdefault(key, []).append(item)
	return {
		key: {
			"count": len(samples),
			"mean_error_seconds": statistics.fmean(samples),
			"mean_absolute_error_seconds": statistics.fmean(
				abs(sample) for sample in samples),
			"rms_error_seconds": (
				statistics.fmean(sample * sample for sample in samples) ** 0.5),
		}
		for key, samples in sorted(values.items())
	}
