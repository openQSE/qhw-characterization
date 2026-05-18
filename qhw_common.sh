#!/usr/bin/env bash

qhw_parse_backend() {
	local backend="auto"
	local expect_backend=0
	local arg

	for arg in "$@"; do
		if [[ "${expect_backend}" -eq 1 ]]; then
			backend="${arg}"
			expect_backend=0
			continue
		fi
		case "${arg}" in
			--backend)
				expect_backend=1
				;;
			--backend=*)
				backend="${arg#--backend=}"
				;;
		esac
	done

	printf '%s\n' "${backend}"
}

qhw_parse_run_id() {
	local run_id=""
	local expect_run_id=0
	local arg

	for arg in "$@"; do
		if [[ "${expect_run_id}" -eq 1 ]]; then
			run_id="${arg}"
			expect_run_id=0
			continue
		fi
		case "${arg}" in
			--run-id)
				expect_run_id=1
				;;
			--run-id=*)
				run_id="${arg#--run-id=}"
				;;
		esac
	done

	if [[ "${expect_run_id}" -eq 1 ]]; then
		echo "ERROR: --run-id requires a value" >&2
		exit 1
	fi

	printf '%s\n' "${run_id}"
}

qhw_args_have_run_id() {
	local expect_run_id=0
	local arg

	for arg in "$@"; do
		if [[ "${expect_run_id}" -eq 1 ]]; then
			return 0
		fi
		case "${arg}" in
			--run-id)
				expect_run_id=1
				;;
			--run-id=*)
				return 0
				;;
		esac
	done

	return 1
}

qhw_args_have_backend() {
	local expect_backend=0
	local arg

	for arg in "$@"; do
		if [[ "${expect_backend}" -eq 1 ]]; then
			return 0
		fi
		case "${arg}" in
			--backend)
				expect_backend=1
				;;
			--backend=*)
				return 0
				;;
		esac
	done

	return 1
}

qhw_init() {
	QHW_REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[1]}")" && pwd)"
	QHW_SERVICES_CONFIG="${QHW_REPO_DIR}/config/qhw_services.yaml"
	QHW_BACKEND="$(qhw_parse_backend "$@")"
	QHW_RUN_ID="$(qhw_parse_run_id "$@")"
	QHW_QFW_STARTED=0
	if [[ -z "${QHW_RUN_ID}" ]]; then
		QHW_RUN_ID="$(date -u +%H%M%S)"
	fi

	QHW_BACKEND_ARGS=()
	if [[ "${QHW_BACKEND}" != "auto" ]]; then
		QHW_BACKEND_ARGS=(--backend "${QHW_BACKEND}")
	fi
}

qhw_use_direct_backend() {
	[[ "${QHW_BACKEND}" == "direct" ]] && return 0
	[[ "${QHW_BACKEND}" == "auto" &&
	   ( -z "${QFW_PATH:-}" || -z "${QFW_SETUP_PATH:-}" ) ]]
}

qhw_require_qfw() {
	if [[ -z "${QFW_PATH:-}" || -z "${QFW_SETUP_PATH:-}" ]]; then
		echo "ERROR: source /path/to/QFw/setup/qfw_activate first" >&2
		exit 1
	fi
}

qhw_teardown() {
	echo "Running QFw teardown..."
	(cd "${QFW_PATH}" && qfw_teardown.sh) || {
		echo "WARNING: qfw_teardown.sh failed" >&2
	}
}

qhw_start_qfw() {
	qhw_require_qfw
	if [[ "${QHW_QFW_STARTED}" -eq 1 ]]; then
		return 0
	fi
	trap qhw_teardown EXIT
	(cd "${QFW_PATH}" && qfw_setup.sh \
		--services-config "${QHW_SERVICES_CONFIG}")
	QHW_QFW_STARTED=1
}

qhw_run_single() {
	local script="$1"
	shift
	local run_id_args=()

	if ! qhw_args_have_run_id "$@"; then
		run_id_args=(--run-id "${QHW_RUN_ID}")
	fi

	if qhw_use_direct_backend; then
		exec python3 "${QHW_REPO_DIR}/${script}" \
			"${run_id_args[@]}" "$@"
	fi

	qhw_start_qfw
	(cd "${QFW_PATH}" && qfw_srun.sh \
		"${QHW_REPO_DIR}/${script}" \
		"${run_id_args[@]}" "$@")
}

qhw_run_python_json() {
	local script="$1"
	shift
	local run_id_args=()
	local backend_args=()

	if ! qhw_args_have_run_id "$@"; then
		run_id_args=(--run-id "${QHW_RUN_ID}")
	fi
	if ! qhw_args_have_backend "$@"; then
		backend_args=("${QHW_BACKEND_ARGS[@]}")
	fi

	python3 "${QHW_REPO_DIR}/${script}" \
		"${backend_args[@]}" "${run_id_args[@]}" "$@" --json
}

qhw_run_qfw_json() {
	local script="$1"
	shift
	local run_id_args=()
	local backend_args=()

	if ! qhw_args_have_run_id "$@"; then
		run_id_args=(--run-id "${QHW_RUN_ID}")
	fi
	if ! qhw_args_have_backend "$@"; then
		backend_args=("${QHW_BACKEND_ARGS[@]}")
	fi

	(cd "${QFW_PATH}" && qfw_srun.sh \
		"${QHW_REPO_DIR}/${script}" \
		"${backend_args[@]}" "${run_id_args[@]}" "$@" --json)
}

qhw_run_suite_json() {
	local script

	if qhw_use_direct_backend; then
		for script in "$@"; do
			qhw_run_python_json "${script}"
		done
		return 0
	fi

	qhw_start_qfw
	for script in "$@"; do
		qhw_run_qfw_json "${script}"
	done
}
