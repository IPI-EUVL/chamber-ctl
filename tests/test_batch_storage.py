from __future__ import annotations

from dataclasses import replace
import threading
import uuid

import pytest

from chamber_ctl.subsystems.batch_storage import BatchManifestRepository
from chamber_ctl.subsystems.batcher import (
    BatchManifest,
    BatchPlan,
    BatchPlanEntry,
    ExecutionMode,
    ExposureTemplate,
    ManifestStatus,
    TargetMode,
)
from ipi_ecs.db.db_library import Library


def _manifest(batch_uuid=None) -> BatchManifest:
    return BatchManifest(
        batch_uuid=batch_uuid or uuid.uuid4(),
        revision=1,
        status=ManifestStatus.DRAFT,
        mode=ExecutionMode.MANUAL,
        plan=BatchPlan(ExposureTemplate("Stored batch"), (BatchPlanEntry(0, TargetMode.DOSE, 10.0),)),
        created_at=100.0,
        updated_at=100.0,
    )


def test_repository_persists_current_manifest_and_append_only_revisions(tmp_path) -> None:
    repository = BatchManifestRepository(str(tmp_path))
    original = _manifest()
    repository.create(original)
    revised = replace(
        original,
        revision=2,
        updated_at=110.0,
        plan=BatchPlan(original.plan.template, (BatchPlanEntry(0, TargetMode.DOSE, 20.0),)),
        revision_note="Raised target",
    )
    repository.save_revision(revised, expected_revision=1)

    assert repository.get(original.batch_uuid) == revised
    assert repository.list() == (revised,)
    assert repository.revisions(original.batch_uuid) == (original, revised)
    repository.close()


def test_repository_rejects_revision_conflicts_and_archives_without_deletion(tmp_path) -> None:
    repository = BatchManifestRepository(str(tmp_path))
    original = _manifest()
    repository.create(original)

    with pytest.raises(ValueError, match="conflict"):
        repository.save_revision(replace(original, revision=2, updated_at=101.0), expected_revision=9)

    cancelled = replace(original, status=ManifestStatus.CANCELLED, updated_at=102.0)
    repository.save_control_state(cancelled)

    assert repository.get(original.batch_uuid).status is ManifestStatus.CANCELLED
    assert len(repository.list()) == 1
    repository.close()


def test_repository_enforces_thread_affinity(tmp_path) -> None:
    repository = BatchManifestRepository(str(tmp_path))
    errors = []

    def read_from_other_thread():
        try:
            repository.list()
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=read_from_other_thread)
    thread.start()
    thread.join()

    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    repository.close()


def test_repository_skips_malformed_manifest_and_reports_the_load_error(tmp_path) -> None:
    repository = BatchManifestRepository(str(tmp_path))
    valid = _manifest()
    repository.create(valid)
    repository.close()

    library = Library(str(tmp_path))
    malformed = library.create_entry("Malformed batch", "Fixture")
    malformed.set_tag("batch_manifest", "1")
    malformed.set_tag("batch_uuid", str(uuid.uuid4()))
    malformed.set_tag("batch_status", ManifestStatus.WITHDRAWN.value)
    with malformed.resource("batch_manifest.json", "batch_manifest", "w") as resource:
        resource.write("{")
    library.close()

    repository = BatchManifestRepository(str(tmp_path))
    try:
        assert repository.list() == (valid,)
        assert len(repository.load_errors) == 1
        assert repository.load_errors[0].status == ManifestStatus.WITHDRAWN.value
    finally:
        repository.close()