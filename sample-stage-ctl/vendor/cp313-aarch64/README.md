# CPython 3.13 aarch64 wheelhouse

This directory contains the complete pinned PyPI dependency set for the
Raspberry Pi release. Normal deployment uses these checked-in artifacts and
does not rebuild them on the target. The steps below are only for maintainers
regenerating the wheelhouse.

Run `scripts/fetch_sample_stage_wheelhouse.ps1` on the workstation to retrieve
universal wheels and the verified `sysv-ipc` source archive.

`sysv-ipc==1.1.0` has no published CPython 3.13 aarch64 wheel. Copy this
directory and `scripts/build_sysv_ipc_wheel.sh` to the Raspberry Pi, run the
builder there, then copy the emitted `sysv_ipc-1.1.0-cp313-*-linux_aarch64.whl`
back into this directory. Re-run the fetcher to generate `SHA256SUMS` only after
that target-built wheel is present.

The release deliberately does not bundle `RPi.GPIO` or `lgpio`. Debian packages
`python3-rpi-lgpio` and `python3-lgpio` provide those target bindings.

The checked-in `sysv_ipc-1.1.0-cp313-cp313-linux_aarch64.whl` was built on the
target Raspberry Pi with CPython headers 3.13.5-1, GCC 14.2.0, glibc 2.41,
setuptools 78.1.1, and wheel 0.46.1. Its SHA-256 is
`13e76b4b4240be1c7b19a087f48e8cdc5c7abf655a1db3cf8db8bcf148198eaa`.
