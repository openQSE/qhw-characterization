"""Utilities for summarizing IQM job timing data."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def parse_timestamp(value: str | None) -> datetime | None:
	if not value:
		return None
	parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
	if parsed.tzinfo is None:
		parsed = parsed.replace(tzinfo=timezone.utc)
	return parsed.astimezone(timezone.utc)


def seconds_between(start: datetime | None,
		    end: datetime | None) -> float | None:
	if start is None or end is None:
		return None
	return (end - start).total_seconds()


def first_time(timeline: list[dict[str, Any]],
	       status: str) -> datetime | None:
	for event in timeline:
		if event.get("status") == status:
			return parse_timestamp(event.get("timestamp"))
	return None


def build_timing_summary(record: dict[str, Any]) -> dict[str, Any]:
	job_data = record.get("job", {}).get("data") or {}
	timeline = job_data.get("timeline") or []
	client_timing = record.get("timing") or {}

	created = first_time(timeline, "created")
	received = first_time(timeline, "received")
	validation_started = first_time(timeline, "validation_started")
	validation_ended = first_time(timeline, "validation_ended")
	compilation_started = first_time(timeline, "compilation_started")
	compilation_ended = first_time(timeline, "compilation_ended")
	execution_started = first_time(timeline, "execution_started")
	execution_ended = first_time(timeline, "execution_ended")
	post_started = first_time(timeline, "post_processing_started")
	post_ended = first_time(timeline, "post_processing_ended")
	ready = first_time(timeline, "ready")
	completed = first_time(timeline, "completed")

	return {
		"schema": "iqm-timing-summary-v1",
		"job_id": record.get("job", {}).get("id"),
		"job_status": record.get("job", {}).get("status"),
		"client_wall_seconds": {
			"submit": client_timing.get("submit_seconds"),
			"wait": client_timing.get("wait_seconds"),
			"result_fetch": client_timing.get("result_fetch_seconds"),
			"total": client_timing.get("total_wall_seconds"),
		},
		"durations_seconds": {
			"server_total_created_to_completed": seconds_between(
				created, completed),
			"created_to_station_received": seconds_between(created, received),
			"queue_wait_received_to_validation_started": seconds_between(
				received, validation_started),
			"validation": seconds_between(
				validation_started, validation_ended),
			"compilation": seconds_between(
				compilation_started, compilation_ended),
			"execution": seconds_between(execution_started, execution_ended),
			"post_processing": seconds_between(post_started, post_ended),
			"ready_to_completed": seconds_between(ready, completed),
			"pre_execution_created_to_execution_started": seconds_between(
				created, execution_started),
		},
		"timeline_events": timeline,
	}

