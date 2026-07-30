#!/bin/sh
set -eu

load_file_env() {
    variable_name="$1"
    file_variable_name="${variable_name}_FILE"
    eval "file_path=\${${file_variable_name}:-}"
    if [ -n "${file_path}" ]; then
        if [ ! -f "${file_path}" ]; then
            echo "required secret file for ${variable_name} is unavailable" >&2
            exit 1
        fi
        value="$(cat "${file_path}")"
        if [ -z "${value}" ]; then
            echo "required secret file for ${variable_name} is empty" >&2
            exit 1
        fi
        export "${variable_name}=${value}"
        unset "${file_variable_name}"
    fi
}

load_file_env DATABASE__URL
load_file_env CHECKPOINT__URL
load_file_env SECURITY__API_KEY
load_file_env SECURITY__METRICS_API_KEY

if [ -n "${PROMETHEUS_MULTIPROC_DIR:-}" ]; then
    if [ "${PROMETHEUS_MULTIPROC_DIR}" != "/var/lib/attacker-prometheus" ]; then
        echo "PROMETHEUS_MULTIPROC_DIR must use the dedicated runtime directory" >&2
        exit 1
    fi
    mkdir -p "${PROMETHEUS_MULTIPROC_DIR}"
    find "${PROMETHEUS_MULTIPROC_DIR}" -mindepth 1 -maxdepth 1 -type f -delete
fi

exec "$@"
