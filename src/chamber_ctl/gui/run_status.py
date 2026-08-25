import time
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
from chamber_ctl import ECS_IP, ECS_PORT
from chamber_ctl.subsystems import uuids
from chamber_ctl.interfaces.scope_interface import PhosphorScopeTk
from chamber_ctl.subsystems.oscilloscope import calculate_dose_of_experiment, DataReader, calculate_dose_of_segment
from chamber_ctl.data.dose_analysis import load_hdf5_snapshot_pulses
from ipi_ecs.subsystems.experiment_controller import ExperimentReader

class ScopeClient:
    __PATH = os.path.join(os.environ["EUVL_PATH"], "datasets")
    def __init__(self, phosphor: PhosphorScopeTk):
        self.__run = True
        self.__remote_kv = None
        self.__phosphor = phosphor

        self.__new_segment_kv = None
        c_uuid = uuid.uuid4()

        self.__logger_sock = tcp.TCPClientSocket()

        self.__logger_sock.connect(("127.0.0.1", 11751))
        self.__logger_sock.start()

        self.__logger = LogClient(self.__logger_sock, origin_uuid=c_uuid)

        self.__did_config = False
        self.__subsystem = None

        self.__segment_queue = queue.Queue()

        def _on_ready():
            if self.__did_config:
                return
            
            self.__did_config = True
            sh = self.__client.register_subsystem("__dose", uuid.uuid4(), temporary=True)

            self.__on_got_subsystem(sh)

        #print("Registering subsystem...")
        self.__client = client.DDSClient(c_uuid, logger=self.__logger, ip=ECS_IP)
        self.__client.when_ready().then(_on_ready)

        self.__daemon = daemon.Daemon()
        self.__daemon.add(self.__calc_thread)
        self.__daemon.start()
        

    def __on_got_subsystem(self, handle: client._RegisteredSubsystemHandle):
        # state has to be named state as it will get passed as a KW argument, stop complaining Pylint!!
        def fail(state, reason): #pylint: disable=unused-argument
            print(f"Failed: {reason}")
            self.__run = False

        handle.get_subsystem(uuids.UUID_EUV_ACQUISITION_CONTROLLER).then(lambda subsystem: subsystem.get_kv(b"new_segment").then(self.__on_got_kv).catch(fail)).catch(fail)

        #pylint: disable=pointless-string-statement

        self.__subsystem = handle

    def __on_got_kv(self, value):
        self.__new_segment_kv = value
        self.__new_segment_kv.on_new_data_received(self.__on_new_segment)

    def ok(self):
        return self.__run and self.__client.ok()
    
    def __calc_thread(self, stop_flag: daemon.StopFlag):
        self.__d_reader = DataReader(self.__PATH)
        self.__hdf5_reader = ExperimentReader(self.__PATH, "exposure")
        total_dose = 0
        total_time = 0
        last_experiment = None

        while stop_flag.run():
            exp, segment = self.__segment_queue.get()
            time.sleep(0.1) # wait a bit for the data to be fully written
            if last_experiment is None or exp != last_experiment:
                print(f"New experiment {exp} detected, resetting total dose.")
                total_dose, total_time = calculate_dose_of_experiment(exp, self.__d_reader)
                last_experiment = exp
                continue
            
            try:
                dose, r_time = calculate_dose_of_segment(exp, segment, self.__d_reader)
                total_dose += dose
                total_time += r_time

                self.__update_phosphor(exp, segment, self.__phosphor)
            except ValueError as e:
                print(f"Error calculating dose for experiment {exp}, segment {segment}: {e}")
                last_experiment = None # reset experiment to force recalculation on next segment
                continue

            print(f"Calculated dose for experiment {exp} is {total_dose} mJ/cm^2 over {total_time:.2f} seconds.")
    
    def __on_new_segment(self, new_data):
        exp, segment = segment_bytes.decode(new_data)

        self.__segment_queue.put((uuid.UUID(bytes=exp), uuid.UUID(bytes=segment)))

    def close(self):
        self.__client.close()
        self.__logger_sock.close()
        self.__daemon.stop()

        self.__run = False

    def __update_phosphor(self, exp, s_uuid, phosphor):
        try:
            record = self.__hdf5_reader.get_run(exp)
            pulses = load_hdf5_snapshot_pulses(record.get_record(), s_uuid)
        except Exception:
            snap, _meta = self.__d_reader.get_snapshot(exp, s_uuid)
            data_f = np.load(snap)
            indexes = data_f["indexes"]
            data = data_f["data"]
            pulse_size = int(indexes[1, 0] - indexes[0, 0])
            pulses = np.reshape(data, (-1, pulse_size, 2))

        for pulse in pulses:
            #cur_time = indexes[n, 1]
            #time.sleep(max(0, cur_time - last_time) * 0.95) # sleep a bit less than the actual time to account for processing time, otherwise we might fall behind over time
            #last_time = cur_time

            phosphor.push([pulse])

def main(args: argparse.Namespace):
    root = tk.Tk()
    root.title("TkAgg Phosphor Pulse Overlay Demo")

    phosphor = PhosphorScopeTk(
        root,
        tlim=(-0e-6, 10e-6),
        vlim=(-0.5, 1.0),
        grid_shape=(200, 500),
        decay=0.98,
        gain=0.8,
        update_ms=20,
    )

    m_client = ScopeClient(phosphor)

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