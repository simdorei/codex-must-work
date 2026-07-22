#!/bin/sh
set -u

bootstrap_prefix='cmw-installer-bootstrap.'
bootstrap=''
temp_root=''

cleanup_bootstrap() {
    if [ -z "$bootstrap" ]; then
        return 0
    fi
    bootstrap_name=${bootstrap##*/}
    case "$bootstrap_name" in
        cmw-installer-bootstrap.?*) ;;
        *)
            echo "installer bootstrap cleanup failed: invalid bootstrap name: $bootstrap" >&2
            return 70
            ;;
    esac
    bootstrap_parent=${bootstrap%/*}
    resolved_parent=$(CDPATH='' cd -- "$bootstrap_parent" 2>/dev/null && pwd -P) || {
        echo "installer bootstrap cleanup failed: temporary parent is unavailable: $bootstrap_parent" >&2
        return 70
    }
    if [ "$resolved_parent" != "$temp_root" ]; then
        echo "installer bootstrap cleanup failed: bootstrap is not a direct child of the temporary root: $bootstrap" >&2
        return 70
    fi
    if [ ! -e "$bootstrap" ] && [ ! -L "$bootstrap" ]; then
        return 0
    fi
    if [ ! -d "$bootstrap" ] || [ -L "$bootstrap" ]; then
        echo "installer bootstrap cleanup failed: bootstrap path was replaced: $bootstrap" >&2
        return 70
    fi
    if ! rm -rf -- "$bootstrap"; then
        echo "installer bootstrap cleanup failed: removal failed: $bootstrap" >&2
        return 70
    fi
    if [ -e "$bootstrap" ] || [ -L "$bootstrap" ]; then
        echo "installer bootstrap cleanup failed: bootstrap remains after removal: $bootstrap" >&2
        return 70
    fi
    return 0
}

finish() {
    status=$?
    trap - 0 HUP INT TERM
    cleanup_bootstrap
    cleanup_status=$?
    if [ "$cleanup_status" -ne 0 ]; then
        status=$cleanup_status
    fi
    exit "$status"
}

trap finish 0
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

case "$0" in
    */*) script_parent=${0%/*} ;;
    *) script_parent=. ;;
esac
source_root=$(CDPATH='' cd -- "$script_parent" && pwd -P) || {
    echo 'installer entrypoint failed: source root is unavailable' >&2
    exit 1
}
launcher="$source_root/runtime/launch-python.sh"
installer="$source_root/scripts/install_plugin.py"
if [ ! -f "$launcher" ]; then
    echo "installer entrypoint failed: portable runtime launcher is missing: $launcher" >&2
    exit 1
fi
if [ ! -f "$installer" ]; then
    echo "installer entrypoint failed: installer script is missing: $installer" >&2
    exit 1
fi

codex_home_input=${CODEX_HOME:-"$HOME/.codex"}
case "$codex_home_input" in
    /*) codex_home=$codex_home_input ;;
    *) codex_home="$(pwd -P)/$codex_home_input" ;;
esac
temp_root_input=${TMPDIR:-/tmp}
temp_root=$(CDPATH='' cd -- "$temp_root_input" && pwd -P) || {
    echo "installer entrypoint failed: temporary root is unavailable: $temp_root_input" >&2
    exit 1
}
bootstrap=$(mktemp -d "$temp_root/$bootstrap_prefix"'XXXXXXXX') || {
    echo "installer entrypoint failed: could not create bootstrap under: $temp_root" >&2
    exit 1
}

CODEX_HOME=$codex_home \
PLUGIN_DATA=$bootstrap \
PYTHONPATH=$source_root \
    /bin/sh "$launcher" "$installer" "$codex_home" "$source_root"
exit $?
