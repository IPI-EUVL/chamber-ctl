import os
import numpy as np

from ipi_ecs.db.db_library import Library
from ipi_ecs.subsystems.experiment_controller import ExperimentReader
from chamber_ctl.subsystems.oscilloscope import DataReader

__SAVE_PATH = os.path.join(os.environ["EUVL_PATH"], "datasets")

def compute_avg_delivery_rate(exp_reader: ExperimentReader):
    """
    Computes the average delivery rate of the last 100 exposures over 5 mj.
    """

    results = exp_reader.query({
        "tags": {
            "dose": {"min": 10},
        }
    }, limit=100)

    print(f"Found {len(results)} experiments with dose tag")

    delivery_rates = []

    for result in results:
        #print("Experiment:", result.get_name())

        dose = result.get_tags().get("dose")
        runtime = result.get_tags().get("runtime")

        #print(f"Dose: {dose} mJ/cm^2, Runtime: {runtime} seconds")

        if dose is not None and runtime is not None and float(runtime) > 0:
            delivery_rate = float(dose) / float(runtime)
            delivery_rates.append(delivery_rate)
            #print(f"Delivery Rate: {delivery_rate * 60} mJ/cm^2/min")

    if len(delivery_rates) > 0:
        avg_delivery_rate = np.mean(delivery_rates)
        print(f"Average Delivery Rate: {avg_delivery_rate * 60} mJ/cm^2/min")

#lib = Library(__SAVE_PATH)
#data_reader = DataReader(__SAVE_PATH)
exp_reader = ExperimentReader(__SAVE_PATH, "exposure")

compute_avg_delivery_rate(exp_reader)