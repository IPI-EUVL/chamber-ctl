import argparse
import os
import queue
import sys
import time

import ipi_ecs.core.daemon as daemon

from ipi_ecs.subsystems.experiment_controller import ExperimentReader
from ipi_ecs.cli.captive_cli import CaptiveCLITemplate

from chamber_ctl.subsystems.exposure_controller import ExposureSettings
from chamber_ctl.subsystems.oscilloscope import DataReader

__SAVE_PATH = os.path.join(os.environ["EUVL_PATH"], "datasets")


class ExperimentDataReader(CaptiveCLITemplate):
    __SAVE_PATH = os.path.join(os.environ["EUVL_PATH"], "datasets")
    
    def __init__(self):
        self.exp_reader = None
        
        self.__lib_thread_in_queue = queue.Queue()
        self.__lib_thread_out_queue = queue.Queue()

        self.__daemon = daemon.Daemon()
        self.__daemon.add(self.__lib_thread)
        self.__daemon.start()
        
        super().__init__("ExperimentReader", "Experiment Reader CLI")

    def __lib_thread(self, stop_flag: daemon.StopFlag):
        self.exp_reader = ExperimentReader(self.__SAVE_PATH, "exposure")
        while stop_flag.run():
            command, pargs, kwargs = self.__lib_thread_in_queue.get()
            result = command(*pargs, **kwargs)
            self.__lib_thread_out_queue.put(result)
            time.sleep(0.01)

    def __do_lib_thread(self, command, *pargs, **kwargs):
        self.__lib_thread_in_queue.put((command, pargs, kwargs))
        return self.__lib_thread_out_queue.get()

    def _build_parser(self, sub: argparse._SubParsersAction, p: argparse.ArgumentParser):
        p_list = sub.add_parser("list", help="List all experiments.")
        p_list.set_defaults(fn=self.list_experiments)

        p_open = sub.add_parser("find", help="Open an experiment by name.")
        p_open.set_defaults(fn=self.find)

    def list_experiments(self, args: argparse.Namespace):
        print("Fetching experiment list...")
        exps = self.__do_lib_thread(self.exp_reader.list_runs)
        print("Experiments:")

        for exp in exps:
            print("Name:", exp.get_name())
            print("Description:", exp.get_description())
            print("Settings:", exp.get_state().get_dict())
            print("Tags:", exp.get_tags())

    def find(self, args: argparse.Namespace):
        def __open_name(name: str):
            return {"name": name}
        
        def __open_by_date(date: str):
            print(f"Finding experiments for date: {date}")
            timestamp_min = time.mktime(time.strptime(date, "%Y-%m-%d"))
            timestamp_max = timestamp_min + 24*3600

            q_args = {
                "created_min": timestamp_min,
                "created_max": timestamp_max,
            }
            return q_args
        
        def __open_today():
            today = time.strftime("%Y-%m-%d")
            print("Finding today's experiments.")
            return __open_by_date(today)
        
        def __query_min_dose(dose: float):
            return {"tags": {"state_dose": {"min": dose, "max": 1e6}}}
        
        def __query_min_target_dose(dose: float):
            return {"tags": {"target_dose": {"min": dose, "max": 1e6}}}
        
        did_set_time_range = False
        def __query_time_range(time_min: float = 0, time_max: float = 1e9):
            nonlocal did_set_time_range
            if not did_set_time_range:
                did_set_time_range = True
                return {"tags": {"state_time": {"min": time_min, "max": time_max}}}
            elif time_min < 1e9 and time_max < 1e9:
                return {"tags": {"state_time": {"min": time_min, "max": time_max}}}
            elif time_min < 1e9:
                return {"tags": {"state_time": {"min": time_min}}}
            elif time_max < 1e9:
                return {"tags": {"state_time": {"max": time_max}}}
        

        print("Finding experiments...")
        print("Query format is the following:")
        print("1)   Enter \"name: experiment_name\" to filter by name.")
        print("2)   Enter \"id: experiment_id\" to filter by ID.")
        print("3)   Enter \"date: YYYY-MM-DD\" to filter by date.")
        print("3a)  Enter \"today\" to filter for today's experiments.")
        print("4)   Enter \"min dose: value\" to filter for experiments with minimum dose.")
        print("4a)  Enter \"min target dose: value\" to filter for experiments with minimum target dose.")
        print("5)   Enter \"min time\" and/or \"max time\" to filter for experiments that lasted a specific duration.")
        print("5a)  Enter \"min time effective\" and/or \"max time effective\" to filter for experiments that lasted a specific effective exposure duration.")
        print("Enter compound queries by separating them with commas, e.g. \"name: test, date: 2024-06-01\".")

        exp_query = input("Enter queries: ")

        compound_query = dict()

        for part in exp_query.split(","):
            part = part.strip()
            if ":" not in part and part.lower() != "today":
                print("Invalid query format.")
                continue

            if ":" in part:
                key, value = part.split(":", 1)
                key = key.strip().lower()
                value = value.strip()
            else:
                key = part.strip().lower()
                value = None

            print("Processing query part:", key, "value:", value)

            if key == "name":
                q_dict = __open_name(value)
            elif key == "date":
                q_dict = __open_by_date(value)
            elif key == "today":
                q_dict = __open_today()
            elif key == "min dose":
                try:
                    dose_value = float(value)
                    q_dict = __query_min_dose(dose_value)
                except ValueError:
                    print("Invalid dose value.")
                    continue
            elif key == "min target dose":
                try:
                    dose_value = float(value)
                    q_dict = __query_min_target_dose(dose_value)
                except ValueError:
                    print("Invalid target dose value.")
                    continue
            elif key == "min time" or key == "max time":
                try:
                    time_value = float(value)
                    if key == "min time":
                        q_dict = __query_time_range(time_min=time_value)
                    else:
                        q_dict = __query_time_range(time_max=time_value)
                except ValueError:
                    print("Invalid time value.")
                    continue
            
            else:
                print("Invalid query key.")
                continue

            compound_query.update(q_dict)

        print("Final compound query:", compound_query)

        if compound_query == {}:
            print("No valid queries entered.")
            return

        exps = self.__do_lib_thread(self.exp_reader.query, compound_query)

        if not exps:
            print("No experiments found.")
            return
        for exp in exps:    
            print("Name:", exp.get_name())
            print("Description:", exp.get_description())
            print("Settings:", exp.get_state().get_dict())
            print("Tags:", exp.get_tags())

        selection_idx = 0

        if len(exps) > 1:
            print(f"Multiple experiments found ({len(exps)}). Select one out of:")
            for i, exp in enumerate(exps):
                print(f"{i}: {exp.get_name()}:{exp.get_description()}")

            selection = input("Enter selection index: ")
            try:
                selection_idx = int(selection)
                if 0 <= selection_idx < len(exps):
                    exp = exps[selection_idx]
                    print("Selected experiment:")
                    print("Name:", exp.get_name())
                    print("Description:", exp.get_description())
                    print("Settings:", exp.get_state().get_dict())
                    print("Tags:", exp.get_tags())
                else:
                    print("Invalid selection index.")
            except ValueError:
                print("Invalid selection input.")
                return
            
        self.__open_experiment = exps[selection_idx]

    def ok(self):
        return self.__daemon.is_ok() & super().ok()

    def close(self):
        self.__daemon.stop()

        super().close()
def main(args: argparse.Namespace):
    m_reader = ExperimentDataReader()

    try:
        while m_reader.ok():
            time.sleep(0.1)

    except KeyboardInterrupt:
        pass
    finally:
        print("Exiting...")
        m_reader.close()
    
    return 0

"""
def main(args: argparse.Namespace):
    exp_reader = ExperimentReader(__SAVE_PATH, "exposure")
    print(type(ExposureSettings))
    exps = exp_reader.list_runs()
    print("Experiments:")

    for exp in exps:
        print(exp)
        print("Name:", exp.get_name())
        print("Description:", exp.get_description())
        print("Settings:", exp.get_state().get_dict())
        print("Tags:", exp.get_tags())
"""
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Target controller CLI interface.")
    args = parser.parse_args()

    sys.exit(main(args))