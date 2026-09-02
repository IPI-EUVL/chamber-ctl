import hashlib
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]


def test_repository_preserves_linux_text_and_binary_artifacts() -> None:
    attributes = (PROJECT_ROOT / ".gitattributes").read_text()

    assert "* text=auto eol=lf" in attributes
    assert "*.ps1 text eol=crlf" in attributes
    assert "*.whl binary" in attributes
    assert "*.gz binary" in attributes


def test_production_unit_uses_immutable_release_as_euvl() -> None:
    unit = (PROJECT_ROOT / "deploy" / "sample_stage.service").read_text()

    assert "User=euvl" in unit
    assert "Group=euvl" in unit
    assert "RuntimeDirectory=sample-stage" in unit
    assert "RuntimeDirectoryMode=0750" in unit
    assert "WorkingDirectory=/run/sample-stage" in unit
    assert 'Environment="PYTHONNOUSERSITE=1"' in unit
    assert 'Environment="PYTHONPATH=/opt/sample-stage/current/python"' in unit
    assert "ExecStart=/usr/bin/python3 -m sample_stage_ctl" in unit


def test_production_unit_preserves_restart_and_shutdown_contract() -> None:
    unit = (PROJECT_ROOT / "deploy" / "sample_stage.service").read_text()

    assert "StartLimitIntervalSec=60" in unit
    assert "StartLimitBurst=5" in unit
    assert "Restart=on-failure" in unit
    assert "RestartSec=2s" in unit
    assert "KillSignal=SIGTERM" in unit
    assert "TimeoutStopSec=10s" in unit


def test_production_unit_omits_pitaya_specific_privileges() -> None:
    unit = (PROJECT_ROOT / "deploy" / "sample_stage.service").read_text()

    assert "User=root" not in unit
    assert "CPUAffinity" not in unit
    assert "LimitRTPRIO" not in unit
    assert "RestrictRealtime" not in unit
    assert "LD_LIBRARY_PATH" not in unit
    assert "EUV_CAPTURE_MODE" not in unit


def test_relay_script_preserves_pitaya_and_owns_only_port_2223() -> None:
    script = (PROJECT_ROOT / "scripts" / "setup_sample_stage_relay.ps1").read_text()

    assert '$natName = "EUVL-Instrument-NAT"' in script
    assert '$pitayaExternalPort = 2222' in script
    assert '$pitayaInternalAddress = "10.11.13.50"' in script
    assert '$sampleStageExternalPort = 2223' in script
    assert '$sampleStageInternalAddress = "10.11.13.225"' in script
    assert "Add-NetNatStaticMapping" in script
    assert "Remove-NetNatStaticMapping -Confirm:$false" in script
    assert "Remove-NetNat -Name" not in script


def test_relay_script_uses_supported_mapping_identity() -> None:
    script = (PROJECT_ROOT / "scripts" / "setup_sample_stage_relay.ps1").read_text()

    assert "StaticMappingName" not in script
    assert "Get-NetNatStaticMapping -NatName $natName" in script
    assert "Remove-NetNatStaticMapping -Confirm:$false" in script


def test_ssh_bootstrap_uses_dedicated_identity_and_alias() -> None:
    script = (PROJECT_ROOT / "scripts" / "setup_sample_stage_ssh.ps1").read_text()

    assert '[string]$Alias = "euvl-sample-stage"' in script
    assert '[string]$HostName = "10.194.210.21"' in script
    assert "[int]$Port = 2223" in script
    assert 'id_ed25519_euvl_sample_stage' in script
    assert "ssh-keygen.exe" in script
    assert "Get-Content $publicKeyPath" in script


def test_dependency_lock_matches_verified_pi_runtime() -> None:
    requirements = set(
        (PROJECT_ROOT / "vendor" / "cp313-aarch64" / "requirements.lock")
        .read_text()
        .splitlines()
    )

    assert "Adafruit-Blinka==8.68.1" in requirements
    assert "adafruit-circuitpython-pcf8574==1.0.15" in requirements
    assert "pyserial==3.5" in requirements
    assert "typing_extensions==4.13.2" in requirements
    assert "sysv-ipc==1.1.0" in requirements
    assert all(not item.startswith("pcf8574-io==") for item in requirements)


def test_target_wheelhouse_is_complete_and_matches_manifest() -> None:
    wheelhouse = PROJECT_ROOT / "vendor" / "cp313-aarch64"
    wheels = sorted(wheelhouse.glob("*.whl"))
    native_wheels = list(
        wheelhouse.glob("sysv_ipc-1.1.0-cp313-*-linux_aarch64.whl")
    )
    manifest_entries = {}
    for line in (wheelhouse / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split("  ", 1)
        manifest_entries[name] = digest

    expected_names = {
        path.name
        for path in wheelhouse.iterdir()
        if path.is_file() and path.name not in {"README.md", "SHA256SUMS"}
    }

    assert len(wheels) == 14
    assert len(native_wheels) == 1
    assert set(manifest_entries) == expected_names
    for name, digest in manifest_entries.items():
        with (wheelhouse / name).open("rb") as artifact:
            assert hashlib.file_digest(artifact, "sha256").hexdigest() == digest


def test_sysv_ipc_builder_is_target_and_version_guarded() -> None:
    script = (PROJECT_ROOT / "scripts" / "build_sysv_ipc_wheel.sh").read_text()

    assert "uname -m) != aarch64" in script
    assert "python_version != 3.13" in script
    assert "sysv_ipc-1.1.0-cp313-*-linux_aarch64.whl" in script
    assert "0f063cbd36ec232032e425769ebc871f195a7d183b9af32f9901589ea7129ac3" in script
    assert "--no-cache-dir" in script
    assert "--no-build-isolation" in script
    assert "--no-index" in script
    assert "--use-pep517" in script


def test_wheelhouse_fetcher_avoids_caches_and_source_build_isolation() -> None:
    script = (
        PROJECT_ROOT / "scripts" / "fetch_sample_stage_wheelhouse.ps1"
    ).read_text()

    assert script.count("--no-cache-dir") == 2
    assert "--no-build-isolation" in script
    assert "--only-binary=:all:" in script
    assert "--no-binary=:all:" in script


def test_deployer_defaults_to_stage_only_and_guards_activation() -> None:
    script = (PROJECT_ROOT / "scripts" / "deploy_sample_stage.ps1").read_text()

    assert '[string]$Target = "euvl-sample-stage"' in script
    assert '$pythonPrefix = @("-3.13")' in script
    assert '"SOURCE_DATE_EPOCH"' in script
    assert '"315532800"' in script
    assert '[string]$ExpectedReleaseId = ""' in script
    assert "$releaseContentLines" in script
    assert "WHEELHOUSE-SHA256SUMS" in script
    assert '"release_content_sha256=$releaseContentHash"' in script
    assert '$releaseId = "$applicationVersion-$($releaseContentHash.Substring(0, 12))"' in script
    assert "if ($RestartService -and -not $ConfirmStageIdle)" in script
    assert '$activation = if ($RestartService) { "restart" } else { "defer" }' in script

    stage_only = script.index('if [ "$activation" = defer ]; then')
    activate = script.index("previous_release=")
    assert stage_only < activate
    assert "exit 0" in script[stage_only:activate]
    assert "STAGED_RELEASE=" in script[stage_only:activate]


def test_deployer_installs_only_verified_offline_wheels() -> None:
    script = (PROJECT_ROOT / "scripts" / "deploy_sample_stage.ps1").read_text()

    assert "sha256sum --check SHA256SUMS" in script
    assert '(Join-Path $bundleRoot "requirements.lock")' in script
    assert "Copy-Item (Join-Path $artifactRoot \"requirements.lock\")" not in script
    assert "--no-cache-dir" in script
    assert "--no-index" in script
    assert "--no-deps" in script
    assert "--root-user-action=ignore" in script
    assert "chmod 0644 sample_stage.service" in script
    assert "grep -Fqx 'RuntimeDirectory=sample-stage'" in script
    assert "grep -Fqx 'WorkingDirectory=/run/sample-stage'" in script
    assert 'test "${#dependency_wheels[@]}" -eq 14' in script
    assert "PYTHONNOUSERSITE=1" in script
    assert "runuser -u euvl" in script
    assert "preflight_runtime=$(mktemp -d /run/sample-stage-preflight.XXXXXX)" in script
    assert 'chown euvl:euvl "$preflight_runtime"' in script
    assert 'rm -rf -- "$preflight_runtime"' in script
    assert 'preflight_runtime="$stage/runtime"' not in script
    assert 'cd "$preflight_runtime"' in script
    assert "installed.locate_file" in script
    assert "import circuitpython_typing" in script
    assert "import binhoHostAdapter" in script
    assert 'cp "$stage/sample_stage.service" "$temporary_release/sample_stage.service"' in script
    assert 'test -f "$release_path/sample_stage.service"' in script


def test_deployer_preflight_matches_verified_target_access() -> None:
    script = (PROJECT_ROOT / "scripts" / "deploy_sample_stage.ps1").read_text()

    assert 'test "$(uname -m)" = aarch64' in script
    assert "python3-rpi-lgpio" in script
    assert "python3-lgpio" in script
    assert "runuser -u euvl -- test -r /dev/gpiomem" in script
    assert "runuser -u euvl -- test -w /dev/gpiomem" in script
    assert "runuser -u euvl -- test -r /dev/i2c-1" in script
    assert "runuser -u euvl -- test -w /dev/i2c-1" in script


def test_deployment_rollback_restores_previous_unit_before_restart() -> None:
    script = (PROJECT_ROOT / "scripts" / "deploy_sample_stage.ps1").read_text()

    restore_unit = script.index('install -m 0644 "$previous_unit" "$unit_path"')
    restore_release = script.index('ln -s "$previous_release" "$rollback_link"')
    restart_service = script.index('systemctl restart "$unit_name"', restore_release)

    assert restore_unit < restore_release < restart_service
    assert 'if [ "$was_enabled" = true ]; then' in script
    assert 'if [ "$was_active" = true ]; then' in script
    assert "systemctl disable" in script
    assert "systemctl stop" in script
    assert 'elif systemctl is-enabled --quiet "$unit_name"; then' in script
    assert 'elif systemctl is-active --quiet "$unit_name"; then' in script
    assert "systemctl disable --now" not in script


def test_deployer_checks_the_udp_listener_local_address() -> None:
    script = (PROJECT_ROOT / "scripts" / "deploy_sample_stage.ps1").read_text()

    assert "awk '$4 ~ /:11755$/" in script
    assert "awk '$5 ~ /:11755$/" not in script
    assert 'systemctl status "$unit_name" --no-pager --full' in script
    assert 'journalctl -u "$unit_name" -n 50 --no-pager' in script