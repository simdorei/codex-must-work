#!/bin/sh
set -eu
export PYTHONUTF8=1

version='3.12.13+20260510'
plugin_root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
data_root=${PLUGIN_DATA:?PLUGIN_DATA is required for the Codex Must Work portable runtime}
platform=$(uname -s)
machine=$(uname -m)

case "$platform:$machine" in
    Linux:x86_64)
        target_name='linux-x64'
        expected_hash='d480f5d5878910ecbae212bf23bd7c25d7b209eb8cf5e98823c977384d272e88'
        ;;
    Darwin:arm64)
        target_name='macos-arm64'
        expected_hash='55bc1a5edbc8ac4da0081f4f5731ed2d1ed10c57cb37a820b2a0dbc7cad742e9'
        ;;
    *)
        echo "unsupported portable runtime target: $platform $machine" >&2
        exit 1
        ;;
esac

archive="$plugin_root/runtime/archives/cpython-$version-$target_name.tar.gz"
target="$data_root/portable-python/$version/$target_name/python"
python="$target/bin/python3"
lock="$data_root/.portable-python.lock"
stage=''
owned_lock=false

mkdir -p "$data_root"
i=0
while ! mkdir "$lock" 2>/dev/null; do
    if [ -x "$python" ]; then
        exec "$python" "$@"
    fi
    i=$((i + 1))
    if [ "$i" -ge 550 ]; then
        echo 'portable runtime bootstrap lock timed out' >&2
        exit 1
    fi
    sleep 0.1
done
owned_lock=true

cleanup() {
    if [ -n "$stage" ] && [ -d "$stage" ]; then
        rm -rf -- "$stage"
    fi
    if [ "$owned_lock" = true ]; then
        rmdir -- "$lock"
    fi
}
trap cleanup EXIT HUP INT TERM

if [ -e "$target" ] && [ ! -x "$python" ]; then
    echo "portable runtime is incomplete: $target" >&2
    exit 1
fi

if [ ! -x "$python" ]; then
    if [ ! -f "$archive" ]; then
        echo "portable runtime archive is missing: $archive" >&2
        exit 1
    fi
    if command -v sha256sum >/dev/null 2>&1; then
        actual_hash=$(sha256sum "$archive" | awk '{print $1}')
    elif command -v shasum >/dev/null 2>&1; then
        actual_hash=$(shasum -a 256 "$archive" | awk '{print $1}')
    else
        echo 'no native SHA-256 checker is available' >&2
        exit 1
    fi
    if [ "$actual_hash" != "$expected_hash" ]; then
        echo "portable runtime archive hash mismatch: $archive" >&2
        exit 1
    fi
    stage=$(mktemp -d "$data_root/.portable-python-stage.XXXXXX")
    tar -xzf "$archive" -C "$stage"
    if [ ! -x "$stage/python/bin/python3" ]; then
        echo 'portable runtime archive has an unexpected layout' >&2
        exit 1
    fi
    mkdir -p "$(dirname -- "$target")"
    mv -- "$stage/python" "$target"
fi

cleanup
trap - EXIT HUP INT TERM
exec "$python" "$@"
