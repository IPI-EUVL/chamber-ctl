#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
project_root=$(cd -- "$script_dir/.." && pwd)
vendor_root="$project_root/vendor/cp313-aarch64"
source_archive=${1:-"$vendor_root/sysv_ipc-1.1.0.tar.gz"}
output_directory=${2:-"$vendor_root"}
expected_source_sha256=0f063cbd36ec232032e425769ebc871f195a7d183b9af32f9901589ea7129ac3

if [[ $(uname -m) != aarch64 ]]; then
    echo "This wheel must be built on aarch64, not $(uname -m)." >&2
    exit 1
fi

python_version=$(/usr/bin/python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
if [[ $python_version != 3.13 ]]; then
    echo "This wheel must be built with CPython 3.13, not $python_version." >&2
    exit 1
fi

command -v gcc >/dev/null
/usr/bin/python3 -c 'import pathlib, sysconfig; assert (pathlib.Path(sysconfig.get_path("include")) / "Python.h").is_file()'
test -f "$source_archive"
printf '%s  %s\n' "$expected_source_sha256" "$source_archive" | sha256sum --check
mkdir -p "$output_directory"

/usr/bin/python3 -m pip wheel \
    --disable-pip-version-check \
    --no-cache-dir \
    --no-build-isolation \
    --no-index \
    --no-deps \
    --use-pep517 \
    --wheel-dir "$output_directory" \
    "$source_archive"

mapfile -t wheels < <(find "$output_directory" -maxdepth 1 -type f \
    -name 'sysv_ipc-1.1.0-cp313-*-linux_aarch64.whl' -print)
if [[ ${#wheels[@]} -ne 1 ]]; then
    echo "Expected one CPython 3.13 aarch64 sysv-ipc wheel; found ${#wheels[@]}." >&2
    exit 1
fi

sha256sum "${wheels[0]}"
