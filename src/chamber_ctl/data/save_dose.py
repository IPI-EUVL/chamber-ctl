import os
import uuid
import argparse
import numpy as np

from ipi_ecs.subsystems.experiment_controller import ExperimentReader
from chamber_ctl.data.dose_analysis import load_experiment_dose_series

def main():
    parser = argparse.ArgumentParser(description="Calculate dose of an experiment.")
    parser.add_argument("experiment_uuid", type=str, help="UUID of the experiment to calculate dose for.")
    parser.add_argument("filename", type=str, help="Name of the output file")
    args = parser.parse_args()

    data_path = os.path.join(os.environ["EUVL_PATH"], "datasets")
    e_uuid = uuid.UUID(args.experiment_uuid)

    exp_reader = ExperimentReader(data_path, "exposure")
    try:
        record = exp_reader.locate_run_by_uuid(e_uuid)
        series = load_experiment_dose_series(e_uuid, record.get_record())
    finally:
        exp_reader.close()

    doses = series.cumulative_dose_mj_cm2
    times = series.cumulative_runtime_seconds

    stack = np.column_stack((times, doses))

    filename = args.filename

    if '.' in filename and not filename.endswith(".npz"):
        print("Warning: Filename has an extension that is not .npz")
    elif not filename.endswith(".npz"):
        filename += ".npz"

    np.savez(filename, stack)

    print(doses, times, stack)

    total_dose = float(doses[-1]) if len(doses) else 0.0
    total_time = float(times[-1]) if len(times) else 0.0
    print(f"Total dose: {total_dose} mJ/cm^2 over {total_time} seconds")
    print(f"Dose and time data saved to {filename} in numpy .npz format")

if __name__ == "__main__":
    main()
