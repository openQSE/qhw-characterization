#!/usr/bin/env bash

set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${repo_dir}/qhw_common.sh"

qhw_run_all_manifest="${repo_dir}/config/qhw_tests.yaml"
qhw_run_all_resolver="${repo_dir}/scripts/qhw_util/run_manifest.py"

qhw_run_all_usage() {
	cat <<'EOF'
Usage: qhw_run_all.sh [--level <level>] [--backend auto|qfw|direct]
       qhw_run_all.sh [<level>] [--backend auto|qfw|direct]

Levels are ordered in config/qhw_tests.yaml. Higher levels include all
tests from lower levels.

Set QHW_RUN_ALL_LEVEL to override the manifest default.
EOF
	if [[ -f "${qhw_run_all_manifest}" ]]; then
		echo
		echo "Configured levels:"
		python3 "${qhw_run_all_resolver}" \
			--manifest "${qhw_run_all_manifest}" levels \
			2>/dev/null | while IFS=$'\t' read -r name description; do
				printf '  %-8s %s\n' "${name}" "${description}"
			done || true
	fi
}

qhw_run_all_parse_level() {
	local expect_level=0
	local expect_backend=0
	local skip_value=0
	local arg

	for arg in "$@"; do
		if [[ "${expect_level}" -eq 1 ]]; then
			qhw_run_all_level="${arg}"
			expect_level=0
			continue
		fi
		if [[ "${expect_backend}" -eq 1 ]]; then
			qhw_run_all_backend_override="${arg}"
			expect_backend=0
			continue
		fi
		if [[ "${skip_value}" -eq 1 ]]; then
			skip_value=0
			continue
		fi

		case "${arg}" in
			-h|--help)
				qhw_run_all_usage
				exit 0
				;;
			--level)
				expect_level=1
				;;
			--level=*)
				qhw_run_all_level="${arg#--level=}"
				;;
			--backend)
				expect_backend=1
				;;
			--backend=*)
				qhw_run_all_backend_override="${arg#--backend=}"
				;;
			--run-id)
				skip_value=1
				;;
			--run-id=*)
				;;
			--*)
				echo "ERROR: unsupported qhw_run_all.sh option: ${arg}" >&2
				qhw_run_all_usage >&2
				exit 1
				;;
			*)
				if [[ -n "${qhw_run_all_positional_level}" ]]; then
					echo "ERROR: only one positional level is supported" >&2
					exit 1
				fi
				qhw_run_all_level="${arg}"
				qhw_run_all_positional_level="${arg}"
				;;
		esac
	done

	if [[ "${expect_level}" -eq 1 ]]; then
		echo "ERROR: --level requires a configured level" >&2
		exit 1
	fi
	if [[ "${expect_backend}" -eq 1 ]]; then
		echo "ERROR: --backend requires a value" >&2
		exit 1
	fi
}

qhw_run_all_validate_plan() {
	local backend_args=()
	if [[ -n "${qhw_run_all_backend_override}" ]]; then
		backend_args=(--backend "${qhw_run_all_backend_override}")
	fi
	python3 "${qhw_run_all_resolver}" \
		--manifest "${qhw_run_all_manifest}" \
		plan --level "${qhw_run_all_level}" \
		"${backend_args[@]}" >/dev/null
}

qhw_run_all_run() {
	local plan_file
	local fields
	local line
	local plan_lines=()
	local script
	local test_args
	local test_backend
	local backend_args=()

	if [[ -n "${qhw_run_all_backend_override}" ]]; then
		backend_args=(--backend "${qhw_run_all_backend_override}")
	fi

	plan_file="$(mktemp)"
	trap 'rm -f "${plan_file}"' RETURN
	python3 "${qhw_run_all_resolver}" \
		--manifest "${qhw_run_all_manifest}" \
		plan --level "${qhw_run_all_level}" \
		"${backend_args[@]}" >"${plan_file}"

	mapfile -t plan_lines <"${plan_file}"

	for line in "${plan_lines[@]}"; do
		IFS=$'\t' read -r -a fields <<<"${line}"
		[[ "${#fields[@]}" -gt 0 ]] || continue
		script="${fields[0]}"
		test_args=("${fields[@]:1}")
		test_backend="$(qhw_parse_backend "${test_args[@]}")"
		if [[ "${test_backend}" == "direct" ]] ||
		   [[ "${test_backend}" == "auto" &&
		      ( -z "${QFW_PATH:-}" || -z "${QFW_SETUP_PATH:-}" ) ]]; then
			qhw_run_python_json "${script}" "${test_args[@]}"
			continue
		fi

		qhw_start_qfw
		qhw_run_qfw_json "${script}" "${test_args[@]}"
	done

	rm -f "${plan_file}"
	trap - RETURN
}

qhw_run_all_positional_level=""
qhw_run_all_backend_override=""
qhw_run_all_level="${QHW_RUN_ALL_LEVEL:-}"
if [[ -z "${qhw_run_all_level}" ]]; then
	qhw_run_all_level="$(python3 "${qhw_run_all_resolver}" \
		--manifest "${qhw_run_all_manifest}" default-level)"
fi
qhw_run_all_parse_level "$@"
qhw_init "$@"
qhw_run_all_validate_plan
qhw_run_all_run
