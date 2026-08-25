import os
import uuid
import argparse
import numpy as np
import plotly.graph_objects as go


from ipi_ecs.subsystems.experiment_controller import ExperimentReader
from chamber_ctl.data.dose_analysis import load_experiment_dose_series

def load_expr(e_uuid: uuid.UUID, exp_reader: ExperimentReader):
    record = exp_reader.locate_run_by_uuid(e_uuid)
    name = f"{record.get_name()}:{record.get_description()}"
    series = load_experiment_dose_series(e_uuid, record.get_record())

    return name, series.cumulative_runtime_seconds, series.cumulative_dose_mj_cm2


def plot_expr(e_uuid: uuid.UUID, exp_reader: ExperimentReader, fig: go.Figure):
    name, times, doses = load_expr(e_uuid, exp_reader)

    fig.add_trace(go.Scatter(x=times, y=doses, mode='lines', name=name))

def main():
    __PATH = os.path.join(os.environ["EUVL_PATH"], "datasets")
    parser = argparse.ArgumentParser(description="Calculate dose of an experiment.")
    parser.add_argument("experiment_uuids", type=str, help="UUIDs of the experiment to calculate dose for.")
    args = parser.parse_args()

    exp_reader = ExperimentReader(__PATH, "exposure")

    fig = go.Figure()

    try:
        for e_uuid_str in args.experiment_uuids.split(","):
            e_uuid = uuid.UUID(e_uuid_str)
            plot_expr(e_uuid, exp_reader, fig)
    finally:
        exp_reader.close()

    fig.show()

if __name__ == "__main__":
    main()
