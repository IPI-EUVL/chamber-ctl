import time
import traceback
import uuid
import sys
import argparse
from ipi_ecs.core import daemon
import segment_bytes
import mt_events
import queue
import os
import numpy as np
import tkinter as tk

import ipi_ecs.dds.client as client
import ipi_ecs.core.tcp as tcp
import ipi_ecs.dds.subsystem as subsystem
import ipi_ecs.dds.types as types
import ipi_ecs.dds.magics as magics

from ipi_ecs.logging.client import LogClient
from chamber_ctl.subsystems import uuids
from chamber_ctl.interfaces.scope_interface import PhosphorScopeTk
from chamber_ctl.subsystems.oscilloscope import calculate_dose_of_experiment, DataReader, calculate_dose_of_segment

class PulseDosageDisplay:
    __PATH = os.path.join(os.environ["EUVL_PATH"], "datasets")
    def __init__(self, root: tk.Tk):
        self.__run = True
        self.__remote_kv = None

        self.__new_segment_kv = None
        c_uuid = uuid.uuid4()

        self.__logger_sock = tcp.TCPClientSocket()

        self.__logger_sock.connect(("127.0.0.1", 11751))
        self.__logger_sock.start()

        self.__logger = LogClient(self.__logger_sock, origin_uuid=c_uuid)

        self.__did_config = False
        self.__subsystem = None

        self.__dose_kv = None

        self.__total_dose = 0
        self.__total_time = 0

        self.__segment_queue = queue.Queue()

        self.__initialize_component(root)

        self.__root = root

        def _on_ready():
            if self.__did_config:
                return
            
            self.__did_config = True
            sh = self.__client.register_subsystem("__dose", uuid.uuid4(), temporary=True)

            self.__on_got_subsystem(sh)

        #print("Registering subsystem...")
        self.__client = client.DDSClient(c_uuid, logger=self.__logger)
        self.__client.when_ready().then(_on_ready)

        self.__daemon = daemon.Daemon(exception_handler=self.handle_exception)
        self.__daemon.add(self.__calc_thread)
        self.__daemon.start()

        self.__update_values() # start the periodic update of the dose label

    def handle_exception(self, e: Exception):
        self.__log("Caught exception on daemon thread!", level="ERROR")
        for line in traceback.format_exception(None, e, e.__traceback__):
            for split in line.split('\n'):
                self.__log(split, level="ERROR")

    def __log(self, msg, level = "INFO", **data):
        if self.__logger is None:
            print(level, msg)
            return
        
        self.__logger.log(msg, level=level, l_type="SW", subsystem="Dosage GUI", **data)

    def __initialize_component(self, root: tk.Tk):
        pulses_frame = tk.LabelFrame(root, text="Pulse Display")
        pulses_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self.__phosphor = PhosphorScopeTk(
            pulses_frame,
            tlim=(-0e-6, 10e-6),
            vlim=(-0.5, 1.0),
            grid_shape=(200, 500),
            decay=0.98,
            gain=0.8,
            update_ms=20,
        )

        dose_frame = tk.LabelFrame(root, text="Dose Information")
        dose_frame.pack(side=tk.BOTTOM, fill=tk.X)

        self.__current_dose_label = tk.Label(dose_frame, text="Current dose: N/A mJ/cm^2", font=("Arial", 36))
        self.__current_dose_label.pack()

    def __update_values(self):
        if self.__total_dose == 0:
            self.__current_dose_label.config(text="No Data")
        else:
            self.__current_dose_label.config(text=f"{self.__total_dose:.2f} mJ/cm^2, {self.__total_time:.2f} s")
        self.__root.after(1000, self.__update_values) # schedule next update

    def __on_got_subsystem(self, handle: client._RegisteredSubsystemHandle):
        # state has to be named state as it will get passed as a KW argument, stop complaining Pylint!!
        def fail(state, reason): #pylint: disable=unused-argument
            print(f"Failed: {reason}")
            self.__run = False

        handle.get_subsystem(uuids.UUID_OSCILLOSCOPE_CONTROLLER).then(lambda subsystem: subsystem.get_kv(b"new_segment").then(self.__on_got_kv).catch(fail)).catch(fail)
        handle.get_subsystem(uuids.UUID_OSCILLOSCOPE_CONTROLLER).then(lambda subsystem: subsystem.get_kv(b"cur_dose").then(self.__on_got_dose_kv).catch(fail)).catch(fail)
        handle.get_subsystem(uuids.UUID_OSCILLOSCOPE_CONTROLLER).then(lambda subsystem: subsystem.get_kv(b"cur_time").then(self.__on_got_time_kv).catch(fail)).catch(fail)

        #pylint: disable=pointless-string-statement

        self.__subsystem = handle

    def __on_got_kv(self, value):
        self.__new_segment_kv = value
        self.__new_segment_kv.on_new_data_received(self.__on_new_segment)

    def __on_got_dose_kv(self, value):
        self.__dose_kv = value
        self.__dose_kv.on_new_data_received(self.__on_new_dose)

    def __on_got_time_kv(self, value):
        self.__time_kv = value
        self.__time_kv.on_new_data_received(self.__on_new_time)

    def ok(self):
        return self.__run and self.__client.ok()
    
    def __calc_thread(self, stop_flag: daemon.StopFlag):
        self.__d_reader = DataReader(self.__PATH)
        #self.__total_dose = 0
        #self.__total_time = 0
        #last_experiment = None

        while stop_flag.run():
            exp, segment = self.__segment_queue.get()
            time.sleep(0.1) # wait a bit for the data to be fully written
            """if last_experiment is None or exp != last_experiment:
                try:
                    self.__logger.log(f"New experiment {exp} detected, resetting total dose.", level="INFO", l_type="SW", subsystem="Dose GUI")
                    self.__total_dose, self.__total_time = calculate_dose_of_experiment(exp, self.__d_reader)
                    last_experiment = exp
                except Exception as e:
                    self.__logger.log(f"Error calculating dose for experiment {exp}: {e}", level="ERROR", l_type="SW", subsystem="Dose GUI")
                    last_experiment = None # reset experiment to force recalculation on next segment
                continue
            
            try:
                dose, r_time = calculate_dose_of_segment(exp, segment, self.__d_reader)
                self.__total_dose += dose
                self.__total_time += r_time

            except (Exception) as e:
                self.__logger.log(f"Error calculating dose for experiment {exp}, segment {segment}: {e}", level="ERROR", l_type="SW", subsystem="Dose GUI")
                last_experiment = None # reset experiment to force recalculation on next segment
                continue
            """
            self.__update_phosphor(exp, segment, self.__phosphor)

            #self.__logger.log(f"Calculated dose for experiment {exp} is {self.__total_dose} mJ/cm^2 over {self.__total_time:.2f} seconds.", level="DEBUG", l_type="EXP", subsystem="Dose GUI")
    
    def __on_new_segment(self, new_data):
        exp, segment = segment_bytes.decode(new_data)

        self.__segment_queue.put((uuid.UUID(bytes=exp), uuid.UUID(bytes=segment)))

    def __on_new_dose(self, new_data):
        self.__total_dose = new_data

    def __on_new_time(self, new_data):
        self.__total_time = new_data

    def close(self):
        self.__client.close()
        self.__logger_sock.close()
        self.__daemon.stop()

        self.__run = False

    def __update_phosphor(self, exp, s_uuid, phosphor):
        try:
            snap, meta = self.__d_reader.get_snapshot(exp, s_uuid)
        except Exception as e:
            self.__logger.log(f"Error loading snapshot for experiment {exp}, segment {s_uuid}: {e}", level="ERROR", l_type="SW", subsystem="Dose GUI")
            return

        data_f = np.load(snap)

        indexes = data_f["indexes"]
        data = data_f["data"]

        pulse_size = int(indexes[1, 0] - indexes[0, 0])
        pulses = np.reshape(data, (-1, pulse_size, 2))

        last_time = indexes[0, 1]
        for n, pulse in enumerate(pulses):
            #cur_time = indexes[n, 1]
            #time.sleep(max(0, cur_time - last_time) * 0.95) # sleep a bit less than the actual time to account for processing time, otherwise we might fall behind over time
            #last_time = cur_time

            self.__phosphor.push([pulse])

def main(args: argparse.Namespace):
    root = tk.Tk()
    root.title("TkAgg Phosphor Pulse Overlay Demo")

    m_client = PulseDosageDisplay(root)

    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        m_client.close()

    return 0

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Subscribe to oscilloscope segments and print them.")
    args = parser.parse_args()

    sys.exit(main(args))