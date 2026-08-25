import os
import uuid
import argparse

from ipi_ecs.subsystems.experiment_controller import ExperimentReader
from chamber_ctl.data.dose_analysis import analyze_experiment_entry


def main():
    parser = argparse.ArgumentParser(description="Calculate dose of an experiment.")
    parser.add_argument("experiment_uuid", type=str, help="UUID of the experiment to calculate dose for.")
    args = parser.parse_args()

    data_path = os.path.join(os.environ["EUVL_PATH"], "datasets")
    run_uuid = uuid.UUID(args.experiment_uuid)
    reader = ExperimentReader(data_path, "exposure")

    print("Starting calculation... This may take a while for long experiments.")
    try:
        record = reader.get_run(run_uuid)
        result = analyze_experiment_entry(run_uuid, record.get_record())
        print("Calculated dose for experiment", args.experiment_uuid, "is:", result.total_dose_mj_cm2, "mJ/cm^2", "over", result.runtime_seconds, "seconds.")
    finally:
        reader.close()

if __name__ == "__main__":
    main()
