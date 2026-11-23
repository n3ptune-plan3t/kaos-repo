#!/usr/bin/env bash

XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
CONF_FILE="${XDG_CONFIG_HOME}/brave-flags.conf"

FLAG_LIST=()
if [[ -r "${CONF_FILE}" ]]; then
    while IFS= read -r line || [[ -n "${line}" ]]; do
        line="${line%%#*}"
        read -ra parts <<< "${line}"
        FLAG_LIST+=("${parts[@]}")
    done < "${CONF_FILE}"
fi

export CHROME_VERSION_EXTRA='stable'

exec /opt/brave/brave "${FLAG_LIST[@]}" "$@"
