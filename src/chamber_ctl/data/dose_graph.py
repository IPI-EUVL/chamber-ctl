import os
import uuid
import argparse
import numpy as np
import plotly.graph_objects as go


from ipi_ecs.subsystems.experiment_controller import ExperimentReader
from chamber_ctl.subsystems.oscilloscope import  calculate_doses_of_segments, DataReader

def load_expr(e_uuid: uuid.UUID, d_reader: DataReader, exp_reader:ExperimentReader):
    name = f"{exp_reader.locate_run_by_uuid(e_uuid).get_name()}:{exp_reader.locate_run_by_uuid(e_uuid).get_description()}"

    abs_doses = []
    abs_times = []

    running_total = 0
    running_time = 0

    doses, times = calculate_doses_of_segments(e_uuid, d_reader)

    print(doses, times)

    for dose, time in zip(doses, times):
        running_total += dose
        running_time += time
        abs_doses.append(running_total)
        abs_times.append(running_time)
    

    return name, abs_times, abs_doses

def plot_expr(e_uuid: uuid.UUID, d_reader: DataReader, exp_reader:ExperimentReader, fig: go.Figure):
    name, times, doses = load_expr(e_uuid, d_reader, exp_reader)

    fig.add_trace(go.Scatter(x=times, y=doses, mode='lines', name=name))

def main():
    __PATH = os.path.join(os.environ["EUVL_PATH"], "datasets")
    parser = argparse.ArgumentParser(description="Calculate dose of an experiment.")
    parser.add_argument("experiment_uuids", type=str, help="UUIDs of the experiment to calculate dose for.")
    args = parser.parse_args()

    d_reader = DataReader(__PATH)
    exp_reader = ExperimentReader(__PATH, "exposure")

    fig = go.Figure()

    for e_uuid_str in args.experiment_uuids.split(","):
        e_uuid = uuid.UUID(e_uuid_str)
        plot_expr(e_uuid, d_reader, exp_reader, fig)

    fig.show()

if __name__ == "__main__":
    main()
