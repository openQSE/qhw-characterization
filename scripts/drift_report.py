#!/usr/bin/env python3
"""Report calibration-quality drift across discover runs."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from qhw_util.args import add_common_arguments
from qhw_util.experiments import write_jsonl
from qhw_util.output import create_run_paths
from qhw_util.output import render_json_output
from qhw_util.output import render_text_output
from qhw_util.output import script_output_path
from qhw_util.output import to_jsonable
from qhw_util.output import write_json
from qhw_util.output import write_script_output


def parse_metric_list(value: str) -> list[str]:
	metrics = [item.strip() for item in value.split(",") if item.strip()]
	if not metrics:
		raise argparse.ArgumentTypeError("metric list must not be empty")
	return metrics


def load_json(path: Path) -> dict[str, Any]:
	with path.open("r", encoding="utf-8") as stream:
		data = json.load(stream)
	if not isinstance(data, dict):
		raise ValueError(f"expected JSON object in {path}")
	return data


def discover_summary_paths(data_dir: Path, start_date: str | None,
			   end_date: str | None) -> list[Path]:
	paths = []
	for path in sorted(data_dir.glob("*/discover/*/calibration_quality_summary.json")):
		date_id = path.parts[-4]
		if start_date and date_id < start_date:
			continue
		if end_date and date_id > end_date:
			continue
		paths.append(path)
	return paths


def record_from_summary(path: Path) -> dict[str, Any]:
	data = load_json(path)
	date_id = path.parts[-4]
	run_id = path.parts[-2]
	metrics = {}
	for name, metric in (data.get("metrics") or {}).items():
		if not isinstance(metric, dict):
			continue
		metrics[name] = {
			"average": metric.get("average"),
			"median": metric.get("median"),
			"unit": metric.get("unit"),
			"display": metric.get("display"),
			"count": metric.get("count"),
			"label": metric.get("label"),
		}
	return {
		"date_id": date_id,
		"run_id": run_id,
		"path": str(path),
		"provider": data.get("provider"),
		"calibration_set_id": data.get("calibration_set_id"),
		"quality_metric_set_id": data.get("quality_metric_set_id"),
		"metrics": metrics,
	}


def metric_delta(first: float | None, last: float | None) -> dict[str, Any]:
	if first is None or last is None:
		return {"absolute": None, "relative": None}
	absolute = last - first
	return {
		"absolute": absolute,
		"relative": absolute / first if first else None,
	}


def build_analysis(records: list[dict[str, Any]],
		   metrics: list[str] | None,
		   relative_threshold: float) -> dict[str, Any]:
	if metrics is None:
		metric_names = sorted({
			name
			for record in records
			for name in record.get("metrics", {})
		})
	else:
		metric_names = metrics

	series = {}
	for name in metric_names:
		points = []
		for record in records:
			metric = record.get("metrics", {}).get(name)
			if not isinstance(metric, dict):
				continue
			points.append({
				"date_id": record["date_id"],
				"run_id": record["run_id"],
				"calibration_set_id": record.get("calibration_set_id"),
				"quality_metric_set_id": record.get("quality_metric_set_id"),
				"average": metric.get("average"),
				"median": metric.get("median"),
				"display": metric.get("display"),
				"unit": metric.get("unit"),
				"label": metric.get("label") or name,
			})
		if not points:
			continue
		first = points[0]
		last = points[-1]
		average_delta = metric_delta(first.get("average"), last.get("average"))
		median_delta = metric_delta(first.get("median"), last.get("median"))
		flagged = any(
			value is not None and abs(value) >= relative_threshold
			for value in (
				average_delta.get("relative"),
				median_delta.get("relative"),
			)
		)
		series[name] = {
			"label": points[0].get("label") or name,
			"points": points,
			"first": first,
			"last": last,
			"average_delta": average_delta,
			"median_delta": median_delta,
			"flagged": flagged,
		}
	return {
		"schema": "qhw-drift-report-v1",
		"intent": (
			"Report changes in discover calibration-quality summaries over "
			"time."),
		"record_count": len(records),
		"metric_count": len(series),
		"relative_threshold": relative_threshold,
		"series": series,
		"flagged_metrics": {
			name: item
			for name, item in series.items()
			if item.get("flagged")
		},
	}


def render_analysis_markdown(analysis: dict[str, Any]) -> str:
	lines = [
		"# Drift Report",
		"",
		analysis["intent"],
		"",
		f"Discover records: {analysis['record_count']}",
		f"Metrics: {analysis['metric_count']}",
		f"Flagged metrics: {len(analysis['flagged_metrics'])}",
		"",
		"| Metric | First average | Last average | Relative delta | Flagged |",
		"| --- | ---: | ---: | ---: | --- |",
	]
	for name, item in analysis["series"].items():
		lines.append(
			f"| `{name}` | {item['first'].get('average')} | "
			f"{item['last'].get('average')} | "
			f"{item['average_delta'].get('relative')} | "
			f"{item['flagged']} |")
	return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(
		description="Build a drift report from discover calibration summaries.")
	parser.add_argument("--data-dir", type=Path, default=Path("data"))
	parser.add_argument("--start-date", default=None)
	parser.add_argument("--end-date", default=None)
	parser.add_argument("--metrics", type=parse_metric_list, default=None)
	parser.add_argument("--relative-threshold", type=float, default=0.05)
	add_common_arguments(
		parser,
		backend=False,
		qfw=False,
		calibration=False,
		execution=False,
		dry_run=False,
	)
	return parser.parse_args()


def main() -> int:
	args = parse_args()
	paths = create_run_paths(__file__, args.output_dir, args.run_id)
	data_dir = args.data_dir
	if not data_dir.is_absolute():
		data_dir = Path(__file__).resolve().parents[1] / data_dir

	summary_paths = discover_summary_paths(
		data_dir, args.start_date, args.end_date)
	records = [record_from_summary(path) for path in summary_paths]

	records_file = paths.results / "drift_records.jsonl"
	analysis_file = paths.results / "analysis.json"
	analysis_md_file = paths.results / "analysis.md"
	summary_file = paths.results / "drift_summary.json"

	write_jsonl(records_file, records)
	analysis = build_analysis(
		records, args.metrics, args.relative_threshold)
	write_json(analysis_file, analysis)
	analysis_md_file.write_text(render_analysis_markdown(analysis))

	summary = {
		"ok": bool(records),
		"date_id": paths.date_id,
		"run_id": paths.run_id,
		"generated_at": datetime.utcnow().isoformat() + "Z",
		"data_dir": str(data_dir),
		"discover_records": len(records),
		"metrics": len(analysis["series"]),
		"flagged_metrics": len(analysis["flagged_metrics"]),
		"output_dir": str(paths.root),
		"files": {
			"records": str(records_file),
			"analysis_json": str(analysis_file),
			"analysis_markdown": str(analysis_md_file),
			"summary": str(summary_file),
			"script_output": str(script_output_path(paths, args.json)),
		},
	}
	write_json(summary_file, summary)

	if args.json:
		output = render_json_output(to_jsonable(summary))
	else:
		output = render_text_output([
			f"discover records: {summary['discover_records']}",
			f"metrics: {summary['metrics']}",
			f"flagged metrics: {summary['flagged_metrics']}",
			f"output: {paths.root}",
		])
	write_script_output(paths, output, args.json)
	return 0 if records else 2


if __name__ == "__main__":
	raise SystemExit(main())
