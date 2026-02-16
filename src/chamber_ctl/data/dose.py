import os
import uuid
import argparse

from chamber_ctl.subsystems.oscilloscope import  calculate_dose_of_experiment, DataReader


def main():
    parser = argparse.ArgumentParser(description="Calculate dose of an experiment.")
    parser.add_argument("experiment_uuid", type=str, help="UUID of the experiment to calculate dose for.")
    args = parser.parse_args()

    d_reader = DataReader(os.path.join(os.environ["EUVL_PATH"], "datasets"))

    print("Starting calculation... This may take a while for long experiments.")
    dose, time = calculate_dose_of_experiment(uuid.UUID(args.experiment_uuid), d_reader)
    print("Calculated dose for experiment", args.experiment_uuid, "is:", dose, "mJ/cm^2", "over", time, "seconds.")

if __name__ == "__main__":
    main()
