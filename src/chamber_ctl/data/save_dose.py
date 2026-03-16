import os
import uuid
import argparse
import numpy as np

from chamber_ctl.subsystems.oscilloscope import  calculate_dose_of_segment, DataReader

def main():
    parser = argparse.ArgumentParser(description="Calculate dose of an experiment.")
    parser.add_argument("experiment_uuid", type=str, help="UUID of the experiment to calculate dose for.")
    parser.add_argument("filename", type=str, help="Name of the output file")
    args = parser.parse_args()

    d_reader = DataReader(os.path.join(os.environ["EUVL_PATH"], "datasets"))
    e_uuid = uuid.UUID(args.experiment_uuid)

    segments = d_reader.get_snapshots(e_uuid)

    running_total = 0
    running_time = 0

    doses = []
    times = []

    for uid, (snapshot_file, snapshot_meta) in segments.items():

        dose, time = calculate_dose_of_segment(e_uuid, uid, d_reader)
        running_total += dose
        running_time += time
        doses.append(running_total)
        times.append(running_time)

    doses = np.array(doses)
    times = np.array(times)

    stack = np.column_stack((times, doses))

    filename = args.filename

    if '.' in filename and not filename.endswith(".npz"):
        print("Warning: Filename has an extension that is not .npz")
    elif not filename.endswith(".npz"):
        filename += ".npz"

    np.savez(filename, stack)

    print(doses, times, stack)

    print(f"Total dose: {running_total} mJ/cm^2 over {running_time} seconds")
    print(f"Dose and time data saved to {filename} in numpy .npz format")

if __name__ == "__main__":
    main()
