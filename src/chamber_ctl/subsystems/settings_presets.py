import queue
import time

import ipi_ecs.core.daemon as daemon

from ipi_ecs.db.db_library import Library

class SettingsPresets:
    def __init__(self, path: str):
        self.__path = path
        self.__library = None

        self.__op_queue = queue.Queue()
        self.__out_queue = queue.Queue()

        self.__daemon = daemon.Daemon()
        self.__daemon.add(self.__lib_thread)
        self.__daemon.start()

    def close(self):
        self.__daemon.stop()

    def __get_or_create_profile_record(self, library: Library):
        recs = library.query({"tags": {"settings_presets": None}, "limit": 1})

        if not recs:
            rec = library.create_entry("Settings Presets", "Saves and loads settings presets for the experiment controller")
            rec.add_tag("settings_presets")
        else:
            rec = recs[0]

        return rec

    def __lib_thread(self, stop_flag: daemon.StopFlag):
        self.__library = Library(self.__path)
        self.__record = self.__get_or_create_profile_record(self.__library)
        
        while stop_flag.run():
            try:
                item = self.__op_queue.get(timeout=0.1)
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

    def __load_data(self, filename: str, type: str) -> list[str]:
        try:
            resource = self.__record.resource(filename, type, "r")
        except FileNotFoundError:
            return []

        data = []
        for line in resource:
            data.append(line.strip())

        resource.close()

        return data
    
    def __save_data(self, filename: str, type: str, data: list[str]) -> None:
        resource = self.__record.resource(filename, type, "w")

        for line in data:
            resource.write(line + "\n")

        resource.close()

    def do_lib_thread(self, func, args):
        self.__op_queue.put((func, args))
        status, result = self.__out_queue.get()
        if status == "ok":
            return result
        else:
            raise result

    def read_sample_types(self) -> list[str]:
        return self.do_lib_thread(self.__load_data, ("sample_types.dat", "Sample Types"))

    def save_sample_types(self, sample_types: list[str]) -> None:
        self.do_lib_thread(self.__save_data, ("sample_types.dat", "Sample Types", sample_types))

    def read_zr_filters(self) -> list[str]:
        return self.do_lib_thread(self.__load_data, ("zr_filters.dat", "Zr Filters"))
    
    def save_zr_filters(self, zr_filters: list[str]) -> None:
        self.do_lib_thread(self.__save_data, ("zr_filters.dat", "Zr Filters", zr_filters))

    def read_operators(self) -> list[str]:
        return self.do_lib_thread(self.__load_data, ("operators.dat", "Operators"))
    
    def save_operators(self, operators: list[str]) -> None:
        self.do_lib_thread(self.__save_data, ("operators.dat", "Operators", operators))
