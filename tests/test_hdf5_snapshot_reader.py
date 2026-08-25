import uuid

import numpy as np
import pytest

from chamber_ctl.data.dose_analysis import HDF5_SNAPSHOT_RESOURCE_TYPE, load_hdf5_snapshot_pulses
from ipi_ecs.db.db_library import Library


def test_hdf5_snapshot_reader_returns_phosphor_compatible_pulse_matrix(tmp_path) -> None:
    import h5py
    import io

    library = Library(tmp_path)
    entry = library.create_entry("Exposure", "Fixture")
    snapshot_id = uuid.uuid4()
    memory = io.BytesIO()
    with h5py.File(memory, "w") as snapshot:
        snapshot.attrs["sample_rate_hz"] = 1_000_000.0
        snapshot.attrs["pretrigger_seconds"] = 1e-6
        snapshot.create_dataset("samples_v", data=np.array([[0.0, 0.2, 0.0]], dtype=np.float32))
    try:
        with entry.resource(f"snap_{snapshot_id}.h5", HDF5_SNAPSHOT_RESOURCE_TYPE, "wb") as resource:
            resource.write(memory.getvalue())
        pulses = load_hdf5_snapshot_pulses(entry, snapshot_id)

        assert pulses.shape == (1, 3, 2)
        assert pulses[0, :, 0].tolist() == [-1e-6, 0.0, 1e-6]
        assert pulses[0, :, 1].tolist() == [0.0, pytest.approx(0.2), 0.0]
    finally:
        library.close()