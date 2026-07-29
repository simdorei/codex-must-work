#!/bin/sh
set -u

bootstrap_prefix='cmw-uninstaller-bootstrap.'
bootstrap=''
temp_root=''

cleanup_bootstrap() {
    if [ -z "$bootstrap" ]; then
        return 0
    fi
    bootstrap_name=${bootstrap##*/}
    case "$bootstrap_name" in
        cmw-uninstaller-bootstrap.?*) ;;
        *)
            echo "uninstaller bootstrap cleanup failed: invalid bootstrap name: $bootstrap" >&2
            return 70
            ;;
    esac
    bootstrap_parent=${bootstrap%/*}
    resolved_parent=$(CDPATH='' cd -- "$bootstrap_parent" 2>/dev/null && pwd -P) || {
        echo "uninstaller bootstrap cleanup failed: temporary parent is unavailable" >&2
        return 70
    }
    if [ "$resolved_parent" != "$temp_root" ]; then
        echo "uninstaller bootstrap cleanup failed: bootstrap escaped temporary root" >&2
        return 70
    fi
    if [ ! -e "$bootstrap" ] && [ ! -L "$bootstrap" ]; then
        return 0
    fi
    if [ ! -d "$bootstrap" ] || [ -L "$bootstrap" ]; then
        echo "uninstaller bootstrap cleanup failed: bootstrap path was replaced" >&2
        return 70
    fi
    rm -rf -- "$bootstrap" || return 70
    if [ -e "$bootstrap" ] || [ -L "$bootstrap" ]; then
        return 70
    fi
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
    echo 'uninstaller entrypoint failed: source root is unavailable' >&2
    exit 1
}
launcher="$source_root/runtime/launch-python.sh"
uninstaller="$source_root/scripts/uninstall_plugin.py"
if [ ! -f "$launcher" ] || [ ! -f "$uninstaller" ]; then
    echo 'uninstaller entrypoint failed: required runtime file is missing' >&2
    exit 1
fi

codex_home_input=${CODEX_HOME:-"$HOME/.codex"}
case "$codex_home_input" in
    /*) codex_home=$codex_home_input ;;
    *) codex_home="$(pwd -P)/$codex_home_input" ;;
esac
temp_root_input=${TMPDIR:-/tmp}
temp_root=$(CDPATH='' cd -- "$temp_root_input" && pwd -P) || {
    echo 'uninstaller entrypoint failed: temporary root is unavailable' >&2
    exit 1
}
bootstrap=$(mktemp -d "$temp_root/$bootstrap_prefix"'XXXXXXXX') || {
    echo 'uninstaller entrypoint failed: could not create bootstrap' >&2
    exit 1
}

purge=''
if [ "${1:-}" = '--purge-data' ] && [ "$#" -eq 1 ]; then
    purge='--purge-data'
elif [ "$#" -ne 0 ]; then
    echo 'usage: uninstall.sh [--purge-data]' >&2
    exit 2
fi

if [ -n "$purge" ]; then
    CODEX_HOME=$codex_home PLUGIN_DATA=$bootstrap PYTHONPATH=$source_root \
        /bin/sh "$launcher" "$uninstaller" "$codex_home" "$source_root" "$purge"
else
    CODEX_HOME=$codex_home PLUGIN_DATA=$bootstrap PYTHONPATH=$source_root \
        /bin/sh "$launcher" "$uninstaller" "$codex_home" "$source_root"
fi
exit $?
