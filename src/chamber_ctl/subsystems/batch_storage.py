from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass

from ipi_ecs.db.db_library import Library

from chamber_ctl.subsystems.batcher import (
    BatchManifest,
    batch_manifest_from_dict,
    batch_manifest_to_dict,
)


MANIFEST_RESOURCE = "batch_manifest.json"
MANIFEST_RESOURCE_TYPE = "batch_manifest"
REVISION_RESOURCE_TYPE = "batch_manifest_revision"


@dataclass(frozen=True)
class BatchManifestLoadError:
    entry_uuid: uuid.UUID
    batch_uuid: str | None
    status: str | None
    error: str


class BatchManifestRepository:
    """Persist batch manifests from one owning worker thread."""

    def __init__(self, data_path: str) -> None:
        self._library = Library(data_path)
        self._thread_id = threading.get_ident()
        self._load_errors: tuple[BatchManifestLoadError, ...] = ()

    def _require_owner(self) -> None:
        if threading.get_ident() != self._thread_id:
            raise RuntimeError("Batch manifest repository used from a non-owning thread.")

    @staticmethod
    def _revision_resource(revision: int) -> str:
        return f"batch_revision_{revision:06d}.json"

    @staticmethod
    def _write_json(entry, filename: str, resource_type: str, value: dict) -> None:
        with entry.resource(filename, resource_type, "w") as resource:
            json.dump(value, resource, allow_nan=False, separators=(",", ":"))

    @staticmethod
    def _read_json(entry, filename: str, resource_type: str) -> dict:
        with entry.resource(filename, resource_type, "r") as resource:
            value = json.load(resource)
        if not isinstance(value, dict):
            raise ValueError(f"Batch resource {filename} must contain a JSON object.")
        return value

    def _find_entry(self, batch_uuid: uuid.UUID):
        entries = self._library.query(
            {"tags": {"batch_manifest": "1", "batch_uuid": str(batch_uuid)}},
            limit=2,
        )
        if len(entries) > 1:
            raise ValueError(f"Multiple manifest records exist for batch {batch_uuid}.")
        return entries[0] if entries else None

    def _update_tags(self, entry, manifest: BatchManifest) -> None:
        entry.set_tag("batch_manifest", "1")
        entry.set_tag("batch_uuid", str(manifest.batch_uuid))
        entry.set_tag("batch_status", manifest.status.value)
        entry.set_tag("batch_mode", manifest.mode.value)
        entry.set_tag("batch_revision", manifest.revision)
        entry.set_tag("batch_updated_at", manifest.updated_at)

    def create(self, manifest: BatchManifest) -> BatchManifest:
        self._require_owner()
        if self._find_entry(manifest.batch_uuid) is not None:
            raise ValueError(f"Batch manifest {manifest.batch_uuid} already exists.")
        entry = self._library.create_entry(
            f"Batch: {manifest.plan.template.name}",
            manifest.plan.template.description or "Exposure batch manifest",
        )
        self._update_tags(entry, manifest)
        value = batch_manifest_to_dict(manifest)
        self._write_json(entry, MANIFEST_RESOURCE, MANIFEST_RESOURCE_TYPE, value)
        self._write_json(
            entry,
            self._revision_resource(manifest.revision),
            REVISION_RESOURCE_TYPE,
            value,
        )
        return manifest

    def get(self, batch_uuid: uuid.UUID) -> BatchManifest | None:
        self._require_owner()
        entry = self._find_entry(batch_uuid)
        if entry is None:
            return None
        return batch_manifest_from_dict(self._read_json(entry, MANIFEST_RESOURCE, MANIFEST_RESOURCE_TYPE))

    def list(self) -> tuple[BatchManifest, ...]:
        self._require_owner()
        entries = self._library.query({"tags": {"batch_manifest": "1"}}, limit=None)
        manifests = []
        errors = []
        for entry in entries:
            try:
                manifests.append(
                    batch_manifest_from_dict(self._read_json(entry, MANIFEST_RESOURCE, MANIFEST_RESOURCE_TYPE))
                )
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                tags = entry.get_tags() or {}
                errors.append(
                    BatchManifestLoadError(
                        entry_uuid=entry.get_uuid(),
                        batch_uuid=None if tags.get("batch_uuid") is None else str(tags["batch_uuid"]),
                        status=None if tags.get("batch_status") is None else str(tags["batch_status"]),
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
        self._load_errors = tuple(errors)
        return tuple(sorted(manifests, key=lambda item: (item.updated_at, str(item.batch_uuid)), reverse=True))

    @property
    def load_errors(self) -> tuple[BatchManifestLoadError, ...]:
        self._require_owner()
        return self._load_errors

    def save_revision(self, manifest: BatchManifest, *, expected_revision: int) -> BatchManifest:
        self._require_owner()
        entry = self._find_entry(manifest.batch_uuid)
        if entry is None:
            raise ValueError(f"Batch manifest {manifest.batch_uuid} was not found.")
        current = batch_manifest_from_dict(self._read_json(entry, MANIFEST_RESOURCE, MANIFEST_RESOURCE_TYPE))
        if current.revision != expected_revision:
            raise ValueError(
                f"Batch revision conflict: expected {expected_revision}, current is {current.revision}."
            )
        if manifest.revision != expected_revision + 1:
            raise ValueError("A plan revision must increment exactly once.")
        revision_filename = self._revision_resource(manifest.revision)
        if revision_filename in dict(entry.list_resources()):
            raise ValueError(f"Batch revision {manifest.revision} already exists.")
        value = batch_manifest_to_dict(manifest)
        self._write_json(entry, revision_filename, REVISION_RESOURCE_TYPE, value)
        self._write_json(entry, MANIFEST_RESOURCE, MANIFEST_RESOURCE_TYPE, value)
        self._update_tags(entry, manifest)
        entry.set_name(f"Batch: {manifest.plan.template.name}")
        entry.set_desc(manifest.plan.template.description or "Exposure batch manifest")
        return manifest

    def save_control_state(self, manifest: BatchManifest) -> BatchManifest:
        self._require_owner()
        entry = self._find_entry(manifest.batch_uuid)
        if entry is None:
            raise ValueError(f"Batch manifest {manifest.batch_uuid} was not found.")
        current = batch_manifest_from_dict(self._read_json(entry, MANIFEST_RESOURCE, MANIFEST_RESOURCE_TYPE))
        if current.revision != manifest.revision:
            raise ValueError("Control state cannot change the current plan revision.")
        self._write_json(
            entry,
            MANIFEST_RESOURCE,
            MANIFEST_RESOURCE_TYPE,
            batch_manifest_to_dict(manifest),
        )
        self._update_tags(entry, manifest)
        return manifest

    def revisions(self, batch_uuid: uuid.UUID) -> tuple[BatchManifest, ...]:
        self._require_owner()
        entry = self._find_entry(batch_uuid)
        if entry is None:
            raise ValueError(f"Batch manifest {batch_uuid} was not found.")
        revision_files = sorted(
            filename
            for filename, resource_type in entry.list_resources()
            if resource_type == REVISION_RESOURCE_TYPE and filename.startswith("batch_revision_")
        )
        return tuple(
            batch_manifest_from_dict(self._read_json(entry, filename, REVISION_RESOURCE_TYPE))
            for filename in revision_files
        )

    def close(self) -> None:
        self._require_owner()
        self._library.close()