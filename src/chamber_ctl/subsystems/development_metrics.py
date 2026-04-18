import uuid
import json
import queue
import math

import ipi_ecs.core.daemon as daemon
from ipi_ecs.subsystems.experiment_controller import ExperimentReader

class DevelopmentMetrics:
    def __init__(self, path: str):
        self.__path = path
        self.__exp_reader = None

        self.__save_queue = queue.Queue()
        self.__out_queue = queue.Queue()

        self.__daemon = daemon.Daemon()
        self.__daemon.add(self.__lib_thread)
        self.__daemon.start()

    def close(self):
        self.__daemon.stop()

    def __lib_thread(self, stop_flag: daemon.StopFlag):
        self.__exp_reader = ExperimentReader(self.__path, "exposure")
        while stop_flag.run():
            try:
                item = self.__save_queue.get(timeout=0.1)
                if item is None:
                    continue

                func, args = item
                try:
                    out = func(*args)
                    self.__out_queue.put(("ok", out))
                except Exception as e:
                    self.__out_queue.put(("err", e))

            except queue.Empty:
                continue

    def __do_save_ellipsometry_data(self, exp_uuid: uuid.UUID, exposed_area_thickness_nm: list[float], blank_area_thickness_nm: list[float], goodness_of_fit: list[float]):
        run = self.__exp_reader.locate_run_by_uuid(exp_uuid)

        file = run.get_record().resource("ellipsometry.json", "Elllipsometry data", "w")
        
        data = {
            "exposed_area_thickness_nm": exposed_area_thickness_nm,
            "blank_area_thickness_nm": blank_area_thickness_nm,
            "goodness_of_fit": goodness_of_fit
        }

        json.dump(data, file)
        file.close()

        exposed_avg = self.__average_finite(exposed_area_thickness_nm)
        blank_avg = self.__average_finite(blank_area_thickness_nm)

        run.add_tag("avg_exposed_area_thickness_nm", exposed_avg)
        run.add_tag("avg_blank_area_thickness_nm", blank_avg)

    @staticmethod
    def __average_finite(values: list[float]):
        finite = []
        for value in values or []:
            try:
                value_f = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value_f):
                finite.append(value_f)

        if not finite:
            return None
        return sum(finite) / len(finite)

    def __do_read_ellipsometry_data(self, exp_uuid: uuid.UUID):
        run = self.__exp_reader.locate_run_by_uuid(exp_uuid)

        file = run.get_record().resource("ellipsometry.json", "Elllipsometry data", "r")
        data = json.load(file)
        file.close()

        return data

    def save_ellipsometry_data(self, exp_uuid: uuid.UUID, exposed_area_thickness_nm: list[float], blank_area_thickness_nm: list[float], goodness_of_fit: list[float]):
        self.__save_queue.put((self.__do_save_ellipsometry_data, (exp_uuid, exposed_area_thickness_nm, blank_area_thickness_nm, goodness_of_fit)))
        status, result = self.__out_queue.get()
        if status == "err":
            raise result
        return result

    def read_ellipsometry_data(self, exp_uuid: uuid.UUID):
        self.__save_queue.put((self.__do_read_ellipsometry_data, (exp_uuid,)))
        status, result = self.__out_queue.get()
        if status == "err":
            raise result
        return result