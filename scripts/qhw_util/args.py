"""Shared command-line argument helpers for hardware test workflows."""

from __future__ import annotations

from pathlib import Path

BACKEND_CHOICES = ("auto", "qfw", "direct")


def add_backend_arguments(parser) -> None:
	parser.add_argument(
		"--backend",
		choices=BACKEND_CHOICES,
		default="auto",
		help=(
			"Execution backend. Default: auto, which uses QFw when "
			"available and falls back to the selected direct provider."
		),
	)
	parser.add_argument(
		"--provider",
		default="iqm",
		help="Direct provider name. Default: iqm.",
	)
	parser.add_argument(
		"--qfw-type",
		default="hardware",
		help=(
			"QFw backend type selector for QFw mode. Use a comma-separated "
			"list such as hardware or hardware,iqm."
		),
	)
	parser.add_argument(
		"--qfw-capability",
		action="append",
		default=None,
		help=(
			"QFw capability selector for QFw mode. May be repeated and may "
			"use comma-separated values. Default: superconducting."
		),
	)


def add_output_arguments(parser) -> None:
	parser.add_argument("--output-dir", type=Path, default=None)
	parser.add_argument("--run-id", default=None)


def add_qfw_arguments(parser) -> None:
	parser.add_argument("--system-up-timeout", type=int, default=40)


def add_calibration_arguments(parser) -> None:
	parser.add_argument("--calibration-set-id", default=None)


def add_execution_arguments(parser) -> None:
	parser.add_argument("--timeout", type=float, default=300.0)
	parser.add_argument("--use-timeslot", action="store_true")


def add_json_argument(parser) -> None:
	parser.add_argument("--json", action="store_true")


def add_dry_run_argument(parser) -> None:
	parser.add_argument("--dry-run", action="store_true")


def add_common_arguments(parser, *,
			 backend: bool = True,
			 output: bool = True,
			 qfw: bool = True,
			 calibration: bool = False,
			 execution: bool = False,
			 json_output: bool = True,
			 dry_run: bool = False) -> None:
	if output:
		add_output_arguments(parser)
	if qfw:
		add_qfw_arguments(parser)
	if calibration:
		add_calibration_arguments(parser)
	if execution:
		add_execution_arguments(parser)
	if dry_run:
		add_dry_run_argument(parser)
	if backend:
		add_backend_arguments(parser)
	if json_output:
		add_json_argument(parser)
