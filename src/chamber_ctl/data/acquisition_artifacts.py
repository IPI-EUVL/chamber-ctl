from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Callable

from chamber_ctl.data.dose_analysis import HDF5_SNAPSHOT_RESOURCE_TYPE


class ArtifactImportError(RuntimeError):
    pass


class AcquisitionArtifactImporter:
    def __init__(self, client, temporary_directory: str | Path | None = None) -> None:
        self.client = client
        self._temporary_directory = None if temporary_directory is None else Path(temporary_directory)

    def import_snapshot(self, record, manifest, *, before_ack: Callable[[object], None] | None = None) -> Path:
        from euv_acquisition.snapshot import read_snapshot

        destination = self._destination_directory()
        try:
            received = self.client.fetch_snapshot(manifest.snapshot_id, destination)
        except Exception as exc:
            raise ArtifactImportError(f"Failed to transfer snapshot {manifest.snapshot_id}: {exc}") from exc
        if received != manifest:
            raise ArtifactImportError(f"Transferred snapshot {manifest.snapshot_id} did not match its requested manifest.")
        local_path = destination / manifest.filename
        try:
            contents = read_snapshot(local_path)
        except Exception as exc:
            raise ArtifactImportError(f"Transferred snapshot {manifest.snapshot_id} failed HDF5 validation: {exc}") from exc
        if contents.snapshot_id != manifest.snapshot_id or contents.session_id != manifest.session_id:
            raise ArtifactImportError(f"Transferred snapshot {manifest.snapshot_id} has mismatched identity.")

        entry = record.get_record() if hasattr(record, "get_record") else record
        try:
            with entry.resource(manifest.filename, HDF5_SNAPSHOT_RESOURCE_TYPE, "wb") as output:
                with local_path.open("rb") as source:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
        except Exception as exc:
            raise ArtifactImportError(f"Failed to write snapshot {manifest.snapshot_id} into the experiment record: {exc}") from exc
        if before_ack is not None:
            try:
                before_ack(entry)
            except Exception as exc:
                raise ArtifactImportError(
                    f"Snapshot {manifest.snapshot_id} was written but its capture timeline could not be persisted: {exc}"
                ) from exc
        try:
            self.client.command("acknowledge_snapshot", {"snapshot_id": str(manifest.snapshot_id)})
        except Exception as exc:
            raise ArtifactImportError(f"Snapshot {manifest.snapshot_id} was written but acknowledgement failed: {exc}") from exc
        return local_path

    def _destination_directory(self) -> Path:
        if self._temporary_directory is None:
            self._temporary_directory = Path(tempfile.mkdtemp(prefix="euv-acquisition-import-"))
        self._temporary_directory.mkdir(parents=True, exist_ok=True)
        return self._temporary_directory