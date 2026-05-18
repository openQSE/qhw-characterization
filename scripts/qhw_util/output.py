"""Output and serialization helpers for hardware test scripts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID
import json

RAW_IQM_KEY = "_raw_iqm"


@dataclass(frozen=True)
class RunPaths:
	root: Path
	circuits: Path
	results: Path
	date_id: str
	run_id: str
	timestamp_utc: str


def to_jsonable(value: Any) -> Any:
	if value is None or isinstance(value, (str, int, float, bool)):
		return value
	if isinstance(value, UUID):
		return str(value)
	if isinstance(value, dict):
		return {str(key): to_jsonable(item) for key, item in value.items()}
	if isinstance(value, (list, tuple, set, frozenset)):
		return [to_jsonable(item) for item in value]
	if is_dataclass(value):
		return to_jsonable(asdict(value))
	if hasattr(value, "model_dump"):
		return to_jsonable(value.model_dump(mode="json"))
	if hasattr(value, "dict"):
		return to_jsonable(value.dict())
	return str(value)


def strip_internal_keys(value: Any) -> Any:
	if isinstance(value, dict):
		return {
			key: strip_internal_keys(item)
			for key, item in value.items()
			if not key.startswith("_qhw_")
		}
	if isinstance(value, list):
		return [strip_internal_keys(item) for item in value]
	return value


def write_json(path: Path, data: Any) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	payload = to_jsonable(data)
	if isinstance(payload, dict) and RAW_IQM_KEY in payload:
		raw_payload = payload.pop(RAW_IQM_KEY)
		raw_path = path.with_suffix(".raw.json")
		raw_path.write_text(json.dumps(
			raw_payload, indent=2, sort_keys=True))
	payload = strip_internal_keys(payload)
	path.write_text(json.dumps(payload, indent=2, sort_keys=True))


def backend_result_qhw(data: Any) -> dict[str, Any]:
	payload = to_jsonable(data)
	if not isinstance(payload, dict):
		return {}
	result = payload.get("result", {})
	if isinstance(result, dict):
		qhw_result = result.get("qhw_result")
		if isinstance(qhw_result, dict):
			return qhw_result
	qhw_result = payload.get("qhw_result")
	return qhw_result if isinstance(qhw_result, dict) else {}


def backend_result_raw(data: Any) -> dict[str, Any]:
	payload = to_jsonable(data)
	if not isinstance(payload, dict):
		return {}
	raw_payload = payload.get(RAW_IQM_KEY)
	return raw_payload if isinstance(raw_payload, dict) else {}


def write_backend_result_artifacts(path: Path, data: Any) -> dict[str, str]:
	path.parent.mkdir(parents=True, exist_ok=True)
	payload = to_jsonable(data)
	raw_payload = backend_result_raw(payload)
	qhw_payload = backend_result_qhw(payload)
	files = {}

	if raw_payload:
		raw_path = path.with_suffix(".raw.json")
		raw_path.write_text(json.dumps(raw_payload, indent=2, sort_keys=True))
		files["raw"] = str(raw_path)

	if qhw_payload:
		qhw_path = path.with_suffix(".qhw.json")
		qhw_path.write_text(json.dumps(
			strip_internal_keys(qhw_payload), indent=2, sort_keys=True))
		files["qhw"] = str(qhw_path)

	if files:
		return files

	raise ValueError(
		"backend circuit results must include a normalized qhw_result")


def script_output_path(paths: RunPaths, json_mode: bool) -> Path:
	name = "stdout.json" if json_mode else "stdout.txt"
	return paths.results / name


def render_json_output(data: Any) -> str:
	return json.dumps(to_jsonable(data), indent=2, sort_keys=True)


def render_text_output(lines: list[str]) -> str:
	return "\n".join(lines)


def write_script_output(paths: RunPaths, data: str, json_mode: bool) -> Path:
	path = script_output_path(paths, json_mode)
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(data if data.endswith("\n") else f"{data}\n")
	print(f"script output: {path}")
	return path


def create_run_paths(script_file: str,
		     output_dir: Path | None = None,
		     run_id: str | None = None) -> RunPaths:
	now = datetime.now(timezone.utc)
	date_id = now.strftime("%Y%m%d")
	run_id = run_id or now.strftime("%H%M%S")
	script_name = Path(script_file).stem

	if output_dir is None:
		repo_dir = Path(script_file).resolve().parents[1]
		root = repo_dir / "data" / date_id / script_name / run_id
	else:
		root = output_dir

	circuits = root / "circuits"
	results = root / "results"
	circuits.mkdir(parents=True, exist_ok=True)
	results.mkdir(parents=True, exist_ok=True)

	return RunPaths(
		root=root,
		circuits=circuits,
		results=results,
		date_id=date_id,
		run_id=run_id,
		timestamp_utc=now.isoformat(),
	)
