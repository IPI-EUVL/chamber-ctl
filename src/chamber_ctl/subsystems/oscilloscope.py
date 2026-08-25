import json
import multiprocessing
import random
import time, struct, os, signal, re, sys, threading
import traceback
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Iterable

try:
    from pyvisa import ResourceManager, errors as visa_errors
except ImportError:
    pass

import socket
import csv
import queue
import math
import tkinter as tk
import numpy as np
#import matplotlib
import segment_bytes
#matplotlib.use("TkAgg")

from scipy import signal
from datetime import datetime

from ipi_ecs.core.daemon import StopFlag, Daemon
from ipi_ecs.logging.client import LogClient

import ipi_ecs.core.tcp as tcp
import ipi_ecs.dds.client as client
import ipi_ecs.dds.subsystem as dds_subsystem
from ipi_ecs.dds.subsystem import StatusItem
import ipi_ecs.dds.types as types
import ipi_ecs.subsystems.experiment_client as exp_client
from ipi_ecs.subsystems.experiment_controller import ExperimentController, ExperimentReader, RunRecord, RunState

import chamber_ctl.subsystems.uuids as uuids
from chamber_ctl.subsystems.exposure_controller import ExposureSettings
from chamber_ctl.subsystems.laser import LaserSyncStatus


class ParallelCalc:
    """Reusable bounded worker pool for independent calculation or I/O tasks."""

    def __init__(self, max_workers: int = 24, *, thread_name_prefix: str = "parallel-calc"):
        if not 1 <= max_workers <= 20:
            raise ValueError("Parallel calculator workers must be between 1 and 20.")
        if not thread_name_prefix:
            raise ValueError("Parallel calculator thread name prefix cannot be empty.")
        self.max_workers = max_workers
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=thread_name_prefix)
        self._lifecycle_lock = threading.Lock()
        self._closed = False

    def submit(self, operation: Callable[..., Any], *args: Any, **kwargs: Any) -> Future:
        with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("Parallel calculator is closed.")
            return self._executor.submit(operation, *args, **kwargs)

    def map(self, operation: Callable[..., Any], *iterables: Iterable[Any]) -> Iterable[Any]:
        with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("Parallel calculator is closed.")
            return self._executor.map(operation, *iterables)

    def close(self, *, wait: bool = True) -> None:
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=wait)

    def __enter__(self) -> "ParallelCalc":
        return self

    def __exit__(self, _exception_type, _exception, _traceback) -> None:
        self.close()


class OscilloscopeStream:
    def start(self):
        pass
       
    def close(self):
        pass

    def ok(self):
        return False
    
    def is_capturing(self):
        return False

    def is_idle(self):
        return not self.is_capturing()
    
    def get_state(self):
        return "STOPPED"
    
    def get_last_capture_time(self):
        return 0
    
    def get_out_queue(self):
        return None
    
    def get_last_start_cmd_time(self):
        return 0
    
    def set_active(self, active: bool):
        pass

    def capture_once(self):
        pass

class ScopeReader(OscilloscopeStream):
    HORI_NUM = 10.0

    PULSE_RATE = 100
    SAVE_RATE = 100
    NUM_SKIP = PULSE_RATE / SAVE_RATE

    # From Siglent example (partial; add more if you use other timebases)
    TDIV_ENUM = [
        100e-12, 200e-12, 500e-12,
        1e-9, 2e-9, 5e-9,
        10e-9, 20e-9, 50e-9,
        100e-9, 200e-9, 500e-9,
        1e-6, 2e-6, 5e-6,
        10e-6, 20e-6, 50e-6,
        100e-6, 200e-6, 500e-6,
        1e-3, 2e-3, 5e-3,
        10e-3, 20e-3, 50e-3,
        100e-3, 200e-3, 500e-3,
        1, 2, 5, 10, 20, 50, 100, 200, 500, 1000,
    ]

    def __init__(self, address):
        self.rm = ResourceManager()
        self.scope = self.rm.open_resource(address)
        self.scope.write_termination = '\n'
        self.scope.read_termination  = None
        self.scope.timeout = 10000

        self.__read_queue = queue.Queue(maxsize=1) # signal from capture to read thread

        self.__is_capturing = False
        self.__is_processing = False
        self.__state = "IDLE"
        self.__last_capturing = 0

        self.__do_capture = False
        self.__do_capture_once = False

        self.__out_queue = queue.Queue() # for processed data

        self.__daemon = Daemon()
        self.__daemon.add(self.__proc_thread)
        self.__daemon.add(self.__read_thread)

    def start(self):
        self.configure()
        self.__daemon.start()

    def set_active(self, active):
        self.__do_capture = active

        if self.is_capturing() and not active:
            self.scope.write(":STOP")

    def capture_once(self):
        print("Oscilloscope capture once triggered.")
        self.__do_capture_once = True

        print("Waiting for capture to start...")
        while not self.__is_capturing:
            self.__do_capture_once = True
            time.sleep(0.01)

        print("Capture started, waiting for it to finish...")

        while self.__is_capturing:
            time.sleep(0.01)

    def configure(self):
        # --- one-time config ---
        self.scope.write(":STOP")
        self.scope.write(":WAVeform:WIDTh BYTE")
        self.scope.write(":WAV:WIDT WORD; :WAV:FORM WORD")
        self.scope.write(":WAVeform:INTerval 10")
        self.scope.write(":ACQ:TYPE NORM")
        self.scope.write(":ACQ:MMAN FSRate")          # fixed sample rate
        self.scope.write(":ACQ:SRAT 2.0E9")           # 10 kS/s
        #self.scope.write(":TIMEBASE:SCAL 0.002; :TIMEBASE:POS 0")  # 2 ms/div
        self.scope.write(":CHAN1:DISP ON; :WAV:SOUR C1")
        self.scope.write(":HISTory ON")              # avoid History interfering with seq
        self.scope.write(":RUN")

    def parse_seq_preamble(self, desc: bytes):
        def u16(o): return struct.unpack("<H", desc[o:o+2])[0]
        def u32(o): return struct.unpack("<I", desc[o:o+4])[0]
        def f32(o): return struct.unpack("<f", desc[o:o+4])[0]
        def f64(o): return struct.unpack("<d", desc[o:o+8])[0]

        width       = u16(0x20)      # 0=BYTE, 1=WORD
        order       = u16(0x22)      # 0=LSB, 1=MSB
        read_pts    = u32(0x74)      # points per frame (this transfer)
        read_frame  = u32(0x90)      # frames in this transfer
        sum_frame   = u32(0x94)      # total acquired frames

        v_scale     = f32(0x9C)      # V/div pre-probe
        v_offset    = f32(0xA0)      # V offset pre-probe
        code_raw    = f32(0xA4)      # codes/div for 16-bit container
        adc_bits_c  = u16(0xAC)      # container bits (16 on HD)

        interval    = f32(0xB0)      # base dt (s/sample)
        delay       = f64(0xB4)      # horiz position
        tdiv_index  = u16(0x144)
        probe       = f32(0x148)
        tdiv = self.TDIV_ENUM[tdiv_index] if 0 <= tdiv_index < len(self.TDIV_ENUM) else None

        vdiv = v_scale * probe
        voff = v_offset * probe

        return {
            "width": width,
            "order": order,
            "read_pts": read_pts,
            "read_frame": read_frame,
            "sum_frame": sum_frame,
            "vdiv": vdiv,
            "voff": voff,
            "code_raw": code_raw,
            "adc_bits_c": adc_bits_c,
            "interval": interval,
            "delay": delay,
            "tdiv": tdiv,
        }


    _EPOCH0 = datetime(1970, 1, 1)
    start_epoch = None
    first_rec_t = None
    last_start_cmd = time.time_ns()

    def _epoch_ns_naive(self, y, m, d, hh, mm, sec_whole, frac_ns):
        """
        Build epoch-ns without any timezone assumptions.
        Treats the scope's date/time as-is.
        """
        # Build base at the start of the minute to avoid 60s edge cases
        base_dt = datetime(y, m, d, hh, mm, 0)
        delta = base_dt - self._EPOCH0
        # Seconds from epoch to minute-start:
        s = delta.days * 86400 + delta.seconds
        return (s + sec_whole) * 1_000_000_000 + frac_ns

    def epochs_ns_from_preamble(self, desc: bytes, n_frames: int):
        """
        SDS2kX HD: parse the per-frame timestamp table from the WAVEDESC preamble.

        The last 16*n_frames bytes of the preamble are the timestamp records:
        [0:8]  seconds-within-minute (float64)
        [8]    minutes  (uint8)
        [9]    hours    (uint8)
        [10]   day      (uint8)
        [11]   month    (uint8)
        [12:14]year     (int16 LE)
        [14:16]reserved

        Returns: list[int] epoch nanoseconds (naive, no tz).
        """
        tail_len = 16 * n_frames
        if len(desc) < tail_len:
            raise ValueError(f"preamble too short: len={len(desc)} < {tail_len}")
        ts_blob = desc[-tail_len:]  # robust across FW variants

        out = []
        for i in range(n_frames):
            rec = ts_blob[16*i : 16*(i+1)]
            secs_f, = struct.unpack("<d", rec[0:8])
            minute  = rec[8]
            hour    = rec[9]
            day     = rec[10]
            month   = rec[11]
            year,   = struct.unpack("<h", rec[12:14])

            # Split fractional seconds into whole + ns, carry if we round to 1e9
            sec_whole = int(secs_f)
            frac_ns = int(round((secs_f - sec_whole) * 1_000_000_000))
            if frac_ns >= 1_000_000_000:
                frac_ns -= 1_000_000_000
                sec_whole += 1

            # Build epoch ns with no timezone logic
            ns = self._epoch_ns_naive(year, month, day, hour, minute, sec_whole, frac_ns)
            out.append(ns)
        return out

    def epochs_ns_zeroed_from_preamble(self, desc: bytes, n_frames: int):
        """
        Same as above, but subtract the first frame's epoch so frame 1 = 0 ns.
        """
        eps = self.epochs_ns_from_preamble(desc, n_frames)
        if not eps:
            return eps
        
        if self.start_epoch is None:
            self.start_epoch = eps[0]

        return [e - self.start_epoch for e in eps]

    def decode_sequence_waveforms(self, desc: bytes, datablock: bytes, wav_int = 100):
        """
        Given one WAVEDESC (desc) and one DATA? payload (data)
        from SDS2000X HD in sequence mode, return:
            t: (n_pts,) time axis [s]
            V: (n_frames, n_pts) voltages [V]
            meta: dict of preamble fields
        """
        m = self.parse_seq_preamble(desc)

        width      = m["width"]
        order      = m["order"]
        read_pts   = m["read_pts"]
        read_frame = m["read_frame"]
        vdiv       = m["vdiv"]
        voff       = m["voff"]
        code_raw   = m["code_raw"]
        interval   = m["interval"]
        delay      = m["delay"]
        tdiv       = m["tdiv"]

        # Export decimation
        N = int(wav_int) if wav_int and wav_int > 0 else 1
        eff_dt = interval * N

        read_pts = int(read_pts / N)

        # ---- bytes/sample ----
        if width == 0:
            bps = 1
        else:
            bps = 2

        # ---- sanity: expected byte length ----
        expected_samples = read_pts * read_frame
        expected_bytes = expected_samples * bps
        if len(datablock) != expected_bytes:
            # If this trips: preamble / read_pts / read_frame mismatch.
            # That will absolutely cause cloned frames.
            raise ValueError(
                f"Length mismatch: got {len(datablock)} bytes, "
                f"expected {expected_bytes} for {read_frame}x{read_pts}"
            )

        # ---- raw → unsigned codes ----
        if width == 0:
            # BYTE export: top 8 bits of 16-bit container (HD quirk)
            adc_bits = 8
            raw = np.frombuffer(datablock, dtype=np.uint8)
            code_per_div = code_raw / (1 << (16 - adc_bits))   # 7680/256 = 30
            center = (1 << (adc_bits - 1)) - 1                 # 127
            full   = 1 << adc_bits                             # 256
        else:
            # WORD export: 16-bit container, 12-bit effective for SDS2000X HD
            adc_bits = 12
            if order == 1:
                raw16 = np.frombuffer(datablock, dtype=">u2")
            else:
                raw16 = np.frombuffer(datablock, dtype="<u2")
            raw = raw16 >> (16 - adc_bits)                     # 16 → 12 bits
            code_per_div = code_raw / (1 << (16 - adc_bits))   # 7680/16 = 480
            center = (1 << (adc_bits - 1)) - 1                 # 2047
            full   = 1 << adc_bits                             # 4096

        if code_per_div == 0:
            print(locals())
            return None, None, None, None

        # ---- reshape strictly by (read_frame, read_pts) ----
        codes_u = raw.reshape(read_frame, read_pts)

        # ---- unsigned → signed ----
        codes = codes_u.astype(np.int32)
        mask = codes > center
        codes[mask] -= full

        # ---- to volts ----
        # V = code * (vdiv / code_per_div) - voff
        V = codes.astype(np.float64) * (vdiv / code_per_div) - voff

        # ---- time axis ----
        idx = np.arange(read_pts, dtype=np.float64)
        if tdiv is not None:
            # Siglent's own formula
            t0 = -delay - (tdiv * self.HORI_NUM / 2.0)
        else:
            t0 = -delay
        t = idx * eff_dt # = t0

        meta = dict(m)
        meta.update({
            "adc_bits": adc_bits,
            "code_per_div_eff": code_per_div,
            "dt_eff": eff_dt,
        })

        timestamps = self.epochs_ns_zeroed_from_preamble(desc, read_frame)
        return t, V, meta, timestamps


    def read_line(self, inst):
        buf = bytearray()
        while True:
            b = inst.read_bytes(1)
            if b == b'\n': return buf.decode('ascii', 'ignore').strip()
            buf += b

    def read_hash_block(self, inst):
        # Skip any "C1:WF ..." prefix until '#'
        while True:
            b = inst.read_bytes(1)
            if b == b'#': break
        nd = int(inst.read_bytes(1).decode())
        n  = int(inst.read_bytes(nd).decode())
        payload = inst.read_bytes(n)
        # eat trailing CR/LF (non-blocking)
        try:
            inst.timeout = 1
            while True:
                c = inst.read_bytes(1)
                if c not in (b'\r', b'\n'): break
        except visa_errors.VisaIOError:
            pass
        finally:
            inst.timeout = 10000
        return payload

    def capture_burst_and_read(self, N=200):
        # Arm segmented capture: exactly N segments, then stop
        self.scope.write(":ACQ:SEQuence ON")
        self.scope.write(f":ACQ:SEQuence:COUNt {N}")
        self.scope.write(":TRIGger:MODE SINGle")
        self.scope.write(":TRIGger:RUN")
        self.last_start_cmd = time.time_ns()
        print(f"Started capture at {self.last_start_cmd} ns")

        if self.first_rec_t is None:
            self.first_rec_t = self.last_start_cmd
        # Wait until stopped (acq done). Poll a light ASCII that changes on stop:

        self.__is_capturing = True

        while True:
            time.sleep(0.02)
            self.scope.write(":TRIG:STAT?")
            st = self.read_line(self.scope)  # e.g., "STOP"
            if "STOP" in st.upper():
                break
            if "READY" in st.upper(): 
                self.__state = "WAITING_TRIGGER"
                continue
            self.__state = "CAPTURING"
            self.__last_capturing = time.time()

        self.__state = "PROC"
        self.__is_capturing = False
        self.__is_processing = True
        # Freeze a consistent snapshot is already ensured (we're stopped)
        self.scope.write(":WAVeform:SEQuence 0,1")         # or 0,<next_start> in your loop
        self.scope.write(":WAVeform:SOURce C1")
        self.scope.write(":WAVeform:PREamble?")
        desc = self.read_hash_block(self.scope)                 # returns WAVEDESC payload

        self.scope.write(":WAVeform:DATA?")
        data = self.read_hash_block(self.scope)                 # returns concatenated frame data

        self.__read_queue.put((self.last_start_cmd, time.time_ns(), desc, data, uuid.uuid4()))

    def __proc_thread(self, stop_flag: StopFlag):
        while stop_flag.run():
            print("Proc thread waiting for read signal...")
            start, end, desc, data, uid = self.__read_queue.get() # wait for signal to read
            print(f"Read thread got data for capture started at {start} ns, ended at {end} ns, processing...")

            # 3) Decode to time + voltages
            t, V, meta, timestamps = self.decode_sequence_waveforms(desc, data, 10)
            if t is None:
                print("Failed to decode waveform data, skipping this capture.")
                self.__is_processing = False
                continue
            
            if len(V) == 0:
                self.__is_processing = False
                continue

            #print(f"Got {len(V)} frames, {len(V[0])} pts/frame. First timestamp: {timestamps[0]} ns. Meta: {meta}")

            indexes = []
            data = np.empty((0, 2))
            cur_time = 0
            for nv in range(0, len(V), int(self.NUM_SKIP)):
                myrealtime = np.copy(timestamps[nv])
                myrealtime += self.start_epoch

                indexes.append((len(data), float(myrealtime) / 1000000000.0))

                time_series = np.linspace(0, meta["interval"] * len(V[nv]) * self.HORI_NUM, len(V[nv]), endpoint=False)

                values = np.column_stack((time_series, V[nv]))
                data = np.vstack((data, values))

            self.__out_queue.put((start, end, data, indexes, uid))
            self.__state = "IDLE"
            self.__is_processing = False

    def __read_thread(self, stop_flag: StopFlag):
        while stop_flag.run():
            #print("Read thread waiting for capture signal...")
            if not self.__do_capture and self.__is_capturing:
                print("Capture deactivated, stopping any ongoing capture...")
                self.scope.write(":STOP")
                self.__state = "IDLE"
                self.__is_capturing = False

            if (not self.__do_capture) and (not self.__do_capture_once):
                time.sleep(0.1)
                continue

            self.__do_capture_once = False
            print("Starting capture...")
            self.capture_burst_and_read(250)
            print("Capture done, waiting for processing to finish...")
       
    def close(self):
        self.scope.write(":ACQ:SEQuence OFF")
        self.scope.write(":STOP")
        self.scope.close()

        self.__daemon.stop()

    def ok(self):
        return self.__daemon.is_ok()
    
    def is_capturing(self):
        return self.__is_capturing

    def is_idle(self):
        return not self.__is_capturing and not self.__is_processing
    
    def get_state(self):
        return self.__state
    
    def get_last_capture_time(self):
        return self.__last_capturing
    
    def get_out_queue(self):
        return self.__out_queue
    
    def get_last_start_cmd_time(self):
        return self.last_start_cmd / 1e9
    
class RealtimeDoseCalc:
    OFF_NUM = 98
    SAMPLE_dT = 10 / 1e9
    RESISTOR_OHMS = 50.0
    RESP_A_PER_W = 0.14
    AREA_CM2 = (5) / 100.0
    REP_RATE_HZ = 100.0

    def __init__(self, osc: ScopeReader, in_queue: queue.Queue = None):
        self.osc = osc

        self.__in_queue = osc.get_out_queue() if in_queue is None else in_queue

        self.__last_laser_on = False
        self.__last_laser_off_time = 0

        self.__daemon = Daemon()
        self.__daemon.add(self.__proc_thread)

        self.cur_dose = 0.0
        self.tot_time = 0.0

    def start(self):
        self.__daemon.start()

    def is_laser_on(self):
        if self.osc.is_capturing() and (time.time() - self.osc.get_last_start_cmd_time()) > 0.25:
            self.__last_laser_on = (time.time() - self.osc.get_last_capture_time()) < 0.5

        return self.__last_laser_on
    
    def __update_laser_status(self):
        if not self.is_laser_on():
            self.__last_laser_off_time = time.time_ns()

    def __proc_thread(self, stop_flag: StopFlag):
        t = time.monotonic()
        # last_laser_on = False

        while stop_flag.run():
            ct = time.monotonic()
            dt = ct - t
            t = ct

            """
            if self.is_laser_on() and not last_laser_on:
                print("Laser has turned ON")
            elif not self.is_laser_on() and last_laser_on:
                print("Laser has turned OFF")
            
            last_laser_on = self.is_laser_on()
            """

            if not self.__in_queue.empty():
                start, data, indexes, uid = self.__in_queue.get() # wait for signal of new data

                # print(self.__last_laser_off_time, start, (self.__last_laser_off_time - start) / 1e9)

                if self.__last_laser_off_time > start:
                    #print("LASER OFF (missed capture)")
                    continue
                
                # Process data to calculate dose in real time
                # Placeholder: just print the first few rows and indexes
                #print("This frame is for ", (time.time_ns() - start) / 1e9, " s after capture start")

                indexes = np.array(indexes)
                data = np.array(data)
                pulse_doses = []
                pulse_webers = []

                pulse_size = int(indexes[1, 0] - indexes[0, 0])
                # print(f"Calculated pulse size: {pulse_size} pts")

                for index, begin_time in indexes:
                    index = int(index)
                    pulse = data[index:index+pulse_size, :]
                    off_avg = np.average(pulse[:self.OFF_NUM, 1])
                    pulse -= off_avg
                    pulsetime = pulse[-1, 0] - pulse[0, 0]
                    auc_webers = np.trapezoid(pulse[:, 1], pulse[:, 0])
                    # print(f"nWeber: {auc_webers * 1e9}") 
                    Q_coulombs = auc_webers / self.RESISTOR_OHMS
                    E_joules = Q_coulombs / self.RESP_A_PER_W
                    E_mJ = E_joules * 1000.0
                    dose_per_pulse_mJ_cm2 = E_mJ / self.AREA_CM2
                    #print(f"uJ/cm2: {dose_per_pulse_mJ_cm2 * 1e3}")
                    pulse_doses.append((begin_time, dose_per_pulse_mJ_cm2))
                    pulse_webers.append((begin_time, auc_webers))

                    #if dose_per_pulse_mJ_cm2 < -1e-5:
                    #    print("NEGATIVE DOSE: ", dose_per_pulse_mJ_cm2)

                pulse_doses = np.array(pulse_doses)
                pulse_webers = np.array(pulse_webers)
                total = np.average(pulse_doses[:, 1])
                total *= ((time.time_ns() - start) / 1e9) * self.REP_RATE_HZ
                # print(f"Total dose = {total} mJ")
                # print(f"Average webers per pulse: {np.average(pulse_webers[:, 1]) * 1e9} nWeber")
                #print(f"Average dose per pulse: {np.average(pulse_doses[:, 1]) * 1e3} uJ/cm2")

                self.cur_dose += total
                #print(f"Cumulative dose: {self.cur_dose} mJ/cm2 over {self.tot_time} s")

            self.__update_laser_status()

            if self.is_laser_on():
                self.tot_time += dt

            time.sleep(0.1)

def calculate_dose_of_experiment(e_uuid: uuid.UUID, d_reader: "DataReader"):
    segments = d_reader.get_snapshots(e_uuid)

    running_total = 0
    running_time = 0

    for uid, (snapshot_file, snapshot_meta) in segments.items():
        try:
            snap_array = np.load(snapshot_file)

            meta = json.load(snapshot_meta)
        except Exception as e:
            print(f"Error loading snapshot {uid}: {e}. Skipping this segment.")
            continue
            
        start = meta["start"]# - (3600 * 6) * 1e9 # adjust for timezone to match scope's naive timestamps
        end = meta["end"]

        print(f"Processing segment {uid} with start={start / 1e9} s, end={end / 1e9} s, duration={(end - start) / 1e9} s")

        if abs(start - end) > 10 * 1e9:
            print(f"Segment {uid} has invalid timestamps: start={start}, end={end}. Skipping.")
            continue

        data = snap_array["data"]
        indexes = snap_array["indexes"]

        try:
            analysis = analyze_snapshot_from_metadata(start, end, data, indexes, meta)

        except Exception as e:
            print(f"Error calculating dose for segment {uid}: {e}. Skipping.")
            continue

        running_total += analysis.total_dose_mj_cm2
        running_time += analysis.runtime_contribution_seconds

    return running_total, running_time

def calculate_doses_of_segments(e_uuid: uuid.UUID, d_reader: "DataReader"):
    segments = d_reader.get_snapshots(e_uuid)

    doses = np.array([])
    times = np.array([])

    for uid, (snapshot_file, snapshot_meta) in segments.items():
        try:
            snap_array = np.load(snapshot_file)

            meta = json.load(snapshot_meta)
        except Exception as e:
            print(f"Error loading snapshot {uid}: {e}. Skipping this segment.")
            continue

        start = meta["start"]# - (3600 * 6) * 1e9 # adjust for timezone to match scope's naive timestamps
        end = meta["end"]

        print(f"Processing segment {uid} with start={start / 1e9} s, end={end / 1e9} s, duration={(end - start) / 1e9} s")

        if abs(start - end) > 10 * 1e9:
            print(f"Segment {uid} has invalid timestamps: start={start}, end={end}. Skipping.")
            continue

        data = snap_array["data"]
        indexes = snap_array["indexes"]

        try:
            analysis = analyze_snapshot_from_metadata(start, end, data, indexes, meta)
        except Exception as e:
            print(f"Error calculating dose for segment {uid}: {e}. Skipping.")
            continue

        doses = np.append(doses, analysis.total_dose_mj_cm2)
        times = np.append(times, analysis.runtime_contribution_seconds
        )

    return doses, times

def calculate_peak_voltages_of_experiment(e_uuid: uuid.UUID, d_reader: "DataReader"):
    segments = d_reader.get_snapshots(e_uuid)

    volts = np.array([])
    times = np.array([])

    for uid, (snapshot_file, snapshot_meta) in segments.items():
        try:
            snap_array = np.load(snapshot_file)

            meta = json.load(snapshot_meta)
        except Exception as e:
            print(f"Error loading snapshot {uid}: {e}. Skipping this segment.")
            continue

        start = meta["start"]# - (3600 * 6) * 1e9 # adjust for timezone to match scope's naive timestamps
        end = meta["end"]

        print(f"Processing segment {uid} with start={start / 1e9} s, end={end / 1e9} s, duration={(end - start) / 1e9} s")

        if abs(start - end) > 10 * 1e9:
            print(f"Segment {uid} has invalid timestamps: start={start}, end={end}. Skipping.")
            continue

        data = snap_array["data"]
        indexes = snap_array["indexes"]

        try:
            peak_volts = calculate_peak_volts(data, indexes)
        except Exception as e:
            print(f"Error calculating peak voltages for segment {uid}: {e}. Skipping.")
            continue

        volts = np.append(volts, peak_volts[:, 1])
        times = np.append(times, peak_volts[:, 0])

    return volts, times

@dataclass(frozen=True)
class SnapshotAnalysis:
    average_pulse_dose_mj_cm2: float
    pulse_times_seconds: np.ndarray
    pulse_indexes: np.ndarray
    pulse_doses_mj_cm2: np.ndarray
    pulse_peaks_volts: np.ndarray
    pulse_span_seconds: float
    wall_duration_seconds: float
    is_step_exposure: bool
    inferred_step_exposure: bool
    effective_duration_seconds: float
    runtime_contribution_seconds: float
    total_dose_mj_cm2: float
    delivered_dose_rate_mj_cm2_s: float


_EXPOSURE_START_UNSPECIFIED = object()


def analyze_snapshot(start, end, data, indexes, is_step_exposure: bool | None = None, *, exposure_start_ns=_EXPOSURE_START_UNSPECIFIED) -> SnapshotAnalysis:
    off_num = 25
    resistor_ohms = 50.0
    responsivity_a_per_w = 0.14
    area_cm2 = 5 / 100.0
    rep_rate_hz = 100.0

    waveform = np.asarray(data, dtype=float)
    pulse_indexes = np.asarray(indexes)
    if waveform.ndim != 2 or waveform.shape[1] < 2:
        raise ValueError("Snapshot data must contain time and voltage columns.")
    if pulse_indexes.ndim != 2 or pulse_indexes.shape[1] < 2:
        raise ValueError("Snapshot indexes must contain sample index and pulse time columns.")
    if not np.isfinite(waveform[:, :2]).all():
        raise ValueError("Snapshot data contains non-finite values.")

    try:
        sample_index_values = np.asarray(pulse_indexes[:, 0], dtype=float)
        pulse_times = np.asarray(pulse_indexes[:, 1], dtype=float)
    except (TypeError, ValueError) as exc:
        raise ValueError("Snapshot indexes must be numeric.") from exc
    if not np.isfinite(sample_index_values).all() or not np.isfinite(pulse_times).all():
        raise ValueError("Snapshot indexes contain non-finite values.")
    if not np.equal(sample_index_values, np.floor(sample_index_values)).all():
        raise ValueError("Snapshot sample indexes must be integers.")
    sample_indexes = sample_index_values.astype(int)
    if np.any(sample_indexes < 0) or np.any(sample_indexes >= len(waveform)):
        raise ValueError("Snapshot sample indexes are outside the waveform.")
    if len(sample_indexes) > 1 and (np.any(np.diff(sample_indexes) <= 0) or np.any(np.diff(pulse_times) < 0)):
        raise ValueError("Snapshot indexes must be strictly increasing with non-decreasing pulse times.")
    if is_step_exposure is not None and not isinstance(is_step_exposure, bool):
        raise ValueError("Snapshot is_step_exposure must be boolean when provided.")

    try:
        start_ns = float(start)
        end_ns = float(end)
        wall_duration = (end_ns - start_ns) / 1e9
    except (TypeError, ValueError) as exc:
        raise ValueError("Snapshot start and end timestamps must be numeric nanoseconds.") from exc
    if not math.isfinite(wall_duration) or wall_duration < 0:
        raise ValueError("Snapshot end timestamp must not precede its start timestamp.")

    pulse_doses = []
    pulse_peaks = []
    for position, sample_index in enumerate(sample_indexes):
        stop = sample_indexes[position + 1] if position + 1 < len(sample_indexes) else len(waveform)
        pulse = waveform[sample_index:stop, :2]
        baseline = float(np.average(pulse[:min(off_num, len(pulse)), 1]))
        corrected_volts = pulse[:, 1] - baseline
        auc_webers = float(np.trapezoid(corrected_volts, pulse[:, 0]))
        dose = ((auc_webers / resistor_ohms) / responsivity_a_per_w) * 1000.0 / area_cm2
        pulse_doses.append(dose)
        pulse_peaks.append(float(np.max(pulse[:, 1])))

    pulse_dose_array = np.asarray(pulse_doses, dtype=float)
    pulse_peak_array = np.asarray(pulse_peaks, dtype=float)
    average_pulse_dose = float(np.average(pulse_dose_array)) if len(pulse_dose_array) else 0.0
    pulse_span = float(pulse_times[-1] - pulse_times[0]) if len(pulse_times) > 1 else 0.0
    inferred_step = len(pulse_dose_array) < 2 or float(np.average(pulse_dose_array[-50:])) < 0.1 * average_pulse_dose
    step_exposure = inferred_step if is_step_exposure is None else is_step_exposure
    effective_duration = pulse_span if step_exposure else wall_duration
    total_dose = average_pulse_dose * effective_duration * rep_rate_hz
    uncorrected_dose = average_pulse_dose * pulse_span * rep_rate_hz
    delivered_rate = uncorrected_dose / pulse_span if pulse_span > 0 else 0.0

    runtime_contribution = effective_duration
    if exposure_start_ns is None:
        runtime_contribution = 0.0
    elif exposure_start_ns is not _EXPOSURE_START_UNSPECIFIED:
        try:
            exposure_start = float(exposure_start_ns)
        except (TypeError, ValueError) as exc:
            raise ValueError("Snapshot exposure start timestamp must be numeric when provided.") from exc
        if not math.isfinite(exposure_start):
            raise ValueError("Snapshot exposure start timestamp must be finite when provided.")
        runtime_contribution = max(0.0, end_ns - max(start_ns, exposure_start)) / 1e9

    return SnapshotAnalysis(
        average_pulse_dose_mj_cm2=average_pulse_dose,
        pulse_times_seconds=pulse_times.copy(),
        pulse_indexes=sample_indexes.copy(),
        pulse_doses_mj_cm2=pulse_dose_array,
        pulse_peaks_volts=pulse_peak_array,
        pulse_span_seconds=pulse_span,
        wall_duration_seconds=wall_duration,
        is_step_exposure=step_exposure,
        inferred_step_exposure=inferred_step,
        effective_duration_seconds=effective_duration,
        runtime_contribution_seconds=runtime_contribution,
        total_dose_mj_cm2=total_dose,
        delivered_dose_rate_mj_cm2_s=delivered_rate,
    )


def analyze_snapshot_from_metadata(start, end, data, indexes, metadata) -> SnapshotAnalysis:
    if not isinstance(metadata, dict):
        raise ValueError("Snapshot metadata must be an object.")

    exposure_start_ns = metadata.get("exposure_start_ns", _EXPOSURE_START_UNSPECIFIED)
    return analyze_snapshot(
        start,
        end,
        data,
        indexes,
        is_step_exposure=metadata.get("is_step_exposure"),
        exposure_start_ns=exposure_start_ns,
    )


def calculate_dose_raw(start, end, data, indexes):
    analysis = analyze_snapshot(start, end, data, indexes)
    return analysis.total_dose_mj_cm2, analysis.effective_duration_seconds


def calculate_peak_volts(data, indexes):
    analysis = analyze_snapshot(0, 0, data, indexes)
    if len(analysis.pulse_indexes) < 2:
        return 0.0, False, 0.0
    return np.column_stack((analysis.pulse_times_seconds, analysis.pulse_peaks_volts))


def calculate_avg_pulsedose(data, indexes):
    analysis = analyze_snapshot(0, 0, data, indexes)
    return (
        analysis.average_pulse_dose_mj_cm2,
        not analysis.is_step_exposure,
        analysis.pulse_span_seconds,
    )

def calculate_dose_of_segment(e_uuid: uuid.UUID, s_uuid: uuid.UUID, d_reader: "DataReader"):
    __PATH = os.path.join(os.environ["EUVL_PATH"], "datasets")

    snapshot_file, snapshot_meta = d_reader.get_snapshot(e_uuid, s_uuid)

    snap_array = np.load(snapshot_file)

    meta = json.load(snapshot_meta)
    start = meta["start"]# - (3600 * 6) * 1e9 # adjust for timezone to match scope's naive timestamps
    end = meta["end"]

    data = snap_array["data"]
    indexes = snap_array["indexes"]

    analysis = analyze_snapshot_from_metadata(start, end, data, indexes, meta)
    return analysis.total_dose_mj_cm2, analysis.runtime_contribution_seconds
    

class DummyOscilloscope(OscilloscopeStream):
    PHASE_EPSILON = 1e-2
    STATUS_MAX_AGE_SECONDS = 0.5

    def __init__(self):
        self.__is_capturing = False
        self.__is_processing = False
        self.__state = "IDLE"
        self.__last_capturing = 0
        self.last_start_cmd = time.time_ns()

        self.__out_queue = queue.Queue() # for processed data

        self.__do_capture = False
        self.__do_capture_once = False

        self.__dds_client = None
        self.__dummy_scope = None
        self.__laser_status_kv = None
        self.__laser_status_lock = threading.Lock()
        self.__laser_status = None
        self.__laser_status_received_at = 0.0

        self.__daemon = Daemon()
        self.__daemon.add(self.__proc_thread)

    def start(self):
        self.__dds_client = client.DDSClient(uuid.uuid4())
        self.__dds_client.when_ready().then(self.__on_dds_ready)
        self.__daemon.start()

    def __on_dds_ready(self):
        if self.__dummy_scope is not None:
            return

        self.__dummy_scope = self.__dds_client.register_subsystem(
            "DummyScope",
            uuid.uuid4(),
            temporary=True,
        )
        self.__laser_status_kv = self.__dummy_scope.add_remote_kv(
            uuids.UUID_LASER_CONTROLLER,
            dds_subsystem.KVDescriptor(types.ByteTypeSpecifier(), b"status", True, True, False),
        )
        self.__laser_status_kv.on_new_data_received(self.__on_laser_status)

    def __on_laser_status(self, payload: bytes):
        try:
            status = LaserSyncStatus.decode(payload)
        except Exception:
            return

        with self.__laser_status_lock:
            self.__laser_status = status
            self.__laser_status_received_at = time.monotonic()

    def __is_transmitting(self) -> bool:
        with self.__laser_status_lock:
            status = self.__laser_status
            received_at = self.__laser_status_received_at

        if status is None or (time.monotonic() - received_at) > self.STATUS_MAX_AGE_SECONDS:
            return False

        try:
            preinit_phase = float(status.preinit_phase)
            open_phase = float(status.configured_target_phase)
            current_phase = float(status.current_phase)
        except (AttributeError, TypeError, ValueError):
            return False

        return (
            status.laser_on
            and status.chopper_on
            and not status.laser_warming_up
            and not status.chopper_starting_up
            and abs(open_phase - preinit_phase) > self.PHASE_EPSILON
            and abs(current_phase - open_phase) <= self.PHASE_EPSILON
        )

    def set_active(self, active):
        print(f"Setting dummy oscilloscope active: {active}")
        self.__do_capture = active

    def capture_once(self):
        print("Dummy oscilloscope capture once triggered.")
        self.__do_capture_once = True

        while not self.__is_capturing:
            time.sleep(0.01)

        while self.__is_capturing:
            time.sleep(0.01)

    def dummy_wf(self):
        timestamp = time.time_ns()
        t = np.linspace(0, 10e-6, num=1000)
        t_square = np.linspace(2*math.pi * 0.5, 2*math.pi * 1.5, num=1000)

        # Add some random offsets to simulate noise and timing jitter
        rand_off_t = random.uniform(-0.0625 * math.pi, 0.06125 * math.pi)
        rand_off_v = random.uniform(-0.1, 0.1)

        t_square += rand_off_t
        if self.__is_transmitting():
            V = ((signal.square(t_square, 0.1) + 1.0) / 2.0) * 0.4
            V += rand_off_v
        else:
            V = np.full_like(t, rand_off_v)

        return t, V, timestamp
    
    def dummy_wfs(self, num):
        ts, Vs, timestamps = [], [], []
        for i in range(num):
            time.sleep(0.01)

            t, V, timestamp = self.dummy_wf()
            self.__last_capturing = time.time()
            ts.append(t)
            Vs.append(V)
            timestamps.append(timestamp)

        return ts, Vs, dict(), timestamps, uuid.uuid4()


    def __proc_thread(self, stop_flag: StopFlag):
        while stop_flag.run():
            if not self.__do_capture and not self.__do_capture_once:
                time.sleep(0.1)
                continue

            self.__do_capture_once = False

            start = time.time_ns()
            self.last_start_cmd = time.time_ns()

            self.__is_capturing = True
            self.__is_processing = True
            self.__state = "CAPTURING"
            #print("Dummy oscilloscope capturing...")
            t, V, meta, timestamps, uid = self.dummy_wfs(250)
            #print("Dummy oscilloscope finished capturing.")

            self.__is_capturing = False
            self.__state = "IDLE"

            indexes = []
            data = np.empty((0, 2))
            cur_time = 0

            #pylint: disable=consider-using-enumerate
            for nv in range(0, len(V)):
                myrealtime = timestamps[nv]
                indexes.append((len(data), float(myrealtime) / 1000000000.0))

                values = np.column_stack((t[nv], V[nv]))
                data = np.vstack((data, values))

            #print("Dummy oscilloscope generated new data, writing...")
            self.__out_queue.put((start, time.time_ns(), data, indexes, uid))
            self.__is_processing = False
       
    def close(self):
        self.__daemon.stop()
        if self.__dds_client is not None:
            self.__dds_client.close()
            self.__dds_client = None

    def ok(self):
        return self.__daemon.is_ok()
    
    def is_capturing(self):
        return self.__is_capturing

    def is_idle(self):
        return not self.__is_capturing and not self.__is_processing
    
    def get_state(self):
        return self.__state
    
    def get_last_capture_time(self):
        return self.__last_capturing
    
    def get_out_queue(self):
        return self.__out_queue
    
    def get_last_start_cmd_time(self):
        return self.last_start_cmd / 1e9
    
class ScopeWriter:
    def __init__(self, logger: LogClient):
        self.__SAVE_PATH = os.path.join(os.environ["EUVL_PATH"], "datasets")
        self.__logger = logger

        self.__exp_reader = None
        self.__record = None
        self.__s_uuid = None

        self.__write_queue = queue.Queue()
        self.__write_condition = threading.Condition()
        self.__pending_writes = 0

        self.__daemon = Daemon()
        self.__daemon.add(self.__writer_thread)
        self.__daemon.start()

    def __writer_thread(self, stop_flag: StopFlag):
        self.__exp_reader = ExperimentReader(self.__SAVE_PATH, "exposure")

        while stop_flag.run():
            try:
                if self.__record is None and self.__s_uuid is not None:
                    self.__do_get_record()

                if not self.__write_queue.empty():
                    start, end, data, indexes, uid, is_step_exposure, exposure_start_ns = self.__write_queue.get()
                    try:
                        self.__write_wf(start, end, data, indexes, uid, is_step_exposure, exposure_start_ns)
                    finally:
                        with self.__write_condition:
                            self.__pending_writes -= 1
                            self.__write_condition.notify_all()
            except Exception as e:
                print(f"Error in ScopeWriter thread: {e}")
                self.__logger.log(f"Error in ScopeWriter thread: {e}", level="ERROR", l_type="EXP", subsystem="Oscilloscope")

            time.sleep(0.1)

    def set_exp_id(self, s_uuid):
        print(f"ScopeWriter set to new experiment UUID: {s_uuid}")
        self.__s_uuid = s_uuid
        self.__record = None

    def __do_get_record(self):
        try:
            self.__record = self.__exp_reader.get_run(self.__s_uuid)
        except ValueError:
            self.__record = None

    def write_wf(self, start, end, data: np.ndarray, indexes, uid, is_step_exposure=False, exposure_start_ns=None):
        if self.__record is None:
            print("No record available for writing, skipping...")
            return False
        
        with self.__write_condition:
            self.__pending_writes += 1
        try:
            self.__write_queue.put((start, end, data, indexes, uid, is_step_exposure, exposure_start_ns))
        except Exception:
            with self.__write_condition:
                self.__pending_writes -= 1
                self.__write_condition.notify_all()
            raise
        return True

    def flush(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        with self.__write_condition:
            while self.__pending_writes:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.__write_condition.wait(remaining)
        return True
    
    def __write_wf(self, start, end, data: np.ndarray, indexes, uid, is_step_exposure=False, exposure_start_ns=None):
        if self.__record is None:
            return
        
        file = self.__record.get_record().resource(f"snap_{str(uid)}.npz", "snapshot", "wb")
        np.savez(file, data=data, indexes=indexes)
        file.close()

        file_meta = self.__record.get_record().resource(f"snap_{str(uid)}.json", "snap_meta", "w")
        meta = {
            "start": start,
            "end": end,
            "num_points": len(data),
            "num_frames": len(indexes),
            "is_step_exposure": is_step_exposure,
            "exposure_start_ns": exposure_start_ns,
        }
        json.dump(meta, file_meta)
        file_meta.close()

    def close(self):
        self.__daemon.stop()

class DataReader:
    def __init__(self, path):
        self.path = path
        self.__exp_reader = ExperimentReader(path, "exposure")

    def close(self):
        self.__exp_reader.close()

    def get_snapshots(self, e_uuid):
        record = self.__exp_reader.get_run(e_uuid)
        if record.get_record() is None:
            print(f"No record found for experiment UUID: {e_uuid}")
            return dict()
        
        resources = record.get_record().list_resources()

        snapshots = dict()

        for fname, f_type in resources:
            if f_type == "snapshot" and fname.endswith(".npz"):
                uid_str = fname[len("snap_"):-len(".npz")]
                print(f"Found snapshot file: {fname}, extracted UID string: {uid_str}")
                try:
                    uid = uuid.UUID(uid_str)
                except ValueError:
                    print(f"Invalid UUID in filename: {fname}, skipping...")
                    continue
                
                try:
                    snapshots[uid] = (record.get_record().resource(fname, "snapshot", "rb"), record.get_record().resource(f"snap_{uid}.json", "snap_meta", "r"))
                except Exception as e:
                    print(f"Error loading snapshot {uid}: {e}. Skipping this segment.")
                    continue

        return snapshots
    
    def get_snapshot(self, e_uuid, uid):
        record = self.__exp_reader.get_run(e_uuid)
        if record.get_record() is None:
            print(f"No record found for experiment UUID: {e_uuid}")
            return None
        
        resources = record.get_record().list_resources()

        for fname, f_type in resources:
            if f_type == "snapshot" and fname.endswith(".npz"):
                uid_str = fname[len("snap_"):-len(".npz")]
                try:
                    file_uid = uuid.UUID(uid_str)
                except ValueError:
                    print(f"Invalid UUID in filename: {fname}, skipping...")
                    continue

                if file_uid == uid:
                    return (record.get_record().resource(fname, "snapshot", "rb"), record.get_record().resource(f"snap_{uid}.json", "snap_meta", "r"))
        
        print(f"No snapshot found for UID: {uid} in experiment UUID: {e_uuid}")
        return None

class OscilloscopeSubsystem(exp_client.ExperimentClient):
    def __init__(self, scope: OscilloscopeStream):
        self.STEP_DOSE_TGT = 1.0 # mJ/cm2, start stepping once dose is within this range of target
        self.__out_queue = queue.Queue()

        self.osc = scope

        self.__osc_queue = self.osc.get_out_queue()
        #self.proc = RealtimeDoseCalc(self.osc, self.__out_queue)

        c_uuid = uuid.uuid4()

        self.__logger_sock = tcp.TCPClientSocket()

        self.__logger_sock.connect(("127.0.0.1", 11751))
        self.__logger_sock.start()

        self.__logger = LogClient(self.__logger_sock, origin_uuid=c_uuid)

        self.__did_config = False
        self.__do_run = False
        self.__did_read = True
        self.__subsystem = None
        self.__status_item_cache = dict()

        self.__exp_id = None
        self.__run_state_lock = threading.Lock()
        self.__exposure_start_ns = None
        self.__exposure_state_kv = None

        self.__preinit_handle = None
        self.__start_handle = None
        self.__stop_handle = None

        self.__writer = ScopeWriter(self.__logger)

        self.__exp_reader = None
        self.__data_reader = None

        self.__dose_queue = queue.Queue()
        self.__finalizing_exp_id = None

        self.__last_laser_on = False
        self.__last_laser_off_time = 0

        self.__current_dose = 0.0
        self.__current_time = 0.0

        self.__target_dose = None
        self.__target_time = None

        self.__dose_publisher = None
        self.__time_publisher = None
        self.__timed_exposure_handle = None
        self.__doing_step_exposure = False

        def _on_ready():
            if self.__did_config:
                return
            
            self.__did_config = True
            sh = self.__client.register_subsystem("Oscilloscope", uuids.UUID_OSCILLOSCOPE_CONTROLLER)

            self.__on_got_subsystem(sh)

        #print("Registering subsystem...")
        self.__client = client.DDSClient(c_uuid, logger=self.__logger)
        self.__client.when_ready().then(_on_ready)

        super().__init__("exposure", "Oscilloscope", self.__logger)
        self.register_experiment_settings_type(ExposureSettings)

        self.__daemon = Daemon(exception_handler=self.handle_exception)
        self.__daemon.add(self.__thread)
        self.__daemon.add(self.__capt_thread)
        self.__daemon.start()

    def handle_exception(self, e: Exception):
        self.__log("Caught exception on daemon thread!", level="ERROR")
        for line in traceback.format_exception(None, e, e.__traceback__):
            for split in line.split('\n'):
                self.__log(split, level="ERROR")
    
    def __log(self, msg, level = "INFO", **data):
        if self.__logger is None:
            print(level, msg)
            return
        
        self.__logger.log(msg, level=level, l_type="SW", subsystem="Oscilloscope", **data)

    def __on_exposure_state(self, payload: bytes):
        try:
            phase_payload, state_payload = segment_bytes.decode(payload)
            phase = int.from_bytes(phase_payload, byteorder="big")
            if phase != ExperimentController.RUN_STATE_RUNNING or not state_payload:
                return
            state = RunState.decode(state_payload.decode("utf-8"))
        except (TypeError, ValueError, UnicodeDecodeError):
            return

        with self.__run_state_lock:
            if self.__exp_id == state.get_uuid() and self.__exposure_start_ns is None:
                self.__exposure_start_ns = time.time_ns()

    def __get_exposure_start_ns(self):
        with self.__run_state_lock:
            return self.__exposure_start_ns

    def __is_laser_on(self):
        if self.osc.is_capturing() and (time.time() - self.osc.get_last_start_cmd_time()) > 0.25:
            self.__last_laser_on = (time.time() - self.osc.get_last_capture_time()) < 0.5

        return self.__last_laser_on

    def __update_laser_status(self):
        if not self.__is_laser_on():
            self.__last_laser_off_time = time.time_ns()

    def __put_status_item_if_changed(self, code: int, severity: int, message: str):
        if self.__subsystem is None:
            return

        status = (severity, message)
        if self.__status_item_cache.get(code) == status:
            return

        self.__subsystem.put_status_item(StatusItem(severity, code, message))
        self.__status_item_cache[code] = status

    def __clear_status_item_if_exists(self, code: int):
        if self.__subsystem is None:
            return

        if self.__subsystem.get_status_item_exists(code):
            self.__subsystem.clear_status_item(code)

        self.__status_item_cache.pop(code, None)

    def __update_status_items(self):
        if self.__doing_step_exposure:
            self.__put_status_item_if_changed(0, StatusItem.STATE_INFO, "Step exposure")
        elif self.__do_run:
            self.__put_status_item_if_changed(0, StatusItem.STATE_INFO, "Running")
        else:
            self.__put_status_item_if_changed(0, StatusItem.STATE_INFO, "Idle")

        laser_on = self.__is_laser_on()
        if self.__do_run and laser_on:
            self.__put_status_item_if_changed(1, StatusItem.STATE_INFO, "Laser on")
            self.__clear_status_item_if_exists(100)
        elif self.__do_run:
            self.__clear_status_item_if_exists(1)
            self.__put_status_item_if_changed(100, StatusItem.STATE_WARN, "Laser off")
        else:
            self.__clear_status_item_if_exists(1)
            self.__clear_status_item_if_exists(100)

        if not self.osc.ok():
            self.__put_status_item_if_changed(101, StatusItem.STATE_WARN, "Oscilloscope not OK")
        else:
            self.__clear_status_item_if_exists(101)

    def __capt_thread(self, stop_flag: StopFlag):
        while stop_flag.run():
            time.sleep(0.1)
            #print("Checking if step exposure should be triggered...")
            #print(f"Current dose: {self.__current_dose} mJ/cm2, target dose: {self.__target_dose} mJ/cm2, do run: {self.__do_run}")
            if self.__do_run and self.__target_dose is not None and self.__current_dose >= (self.__target_dose - self.STEP_DOSE_TGT) and self.__current_dose < self.__target_dose:
                self.__doing_step_exposure = True
                try:
                    #print(f"Current dose {self.__current_dose} mJ/cm2 is within {self.__STEP_DOSE_TGT} mJ/cm2 of target dose {self.__target_dose} mJ/cm2, starting to do step exposures...")
                    self.__logger.log(f"Current dose {self.__current_dose} mJ/cm2 is within {self.STEP_DOSE_TGT} mJ/cm2 of target dose {self.__target_dose} mJ/cm2, starting to do step exposures...", level="INFO", l_type="EXP", subsystem="Oscilloscope", action="BEGIN_STEP_EXPOSURE", exp_id=str(self.__exp_id), current_dose=self.__current_dose, target_dose=self.__target_dose)
                    self.osc.set_active(False)

                    time.sleep(0.1)
                    while self.osc.is_capturing():
                        time.sleep(0.1)
                    
                    time.sleep(0.1)
                    payload = struct.pack('d', float(0.5))
                    self.__timed_exposure_handle = self.__do_timed_exposure_event.call(payload, [uuids.UUID_LASER_CONTROLLER])
                    self.__did_read = False
                    self.osc.capture_once()
                    time.sleep(0.1)

                    start_t = time.monotonic()

                    while self.__timed_exposure_handle.is_in_progress() and (time.monotonic() - start_t) < 10.0:
                        time.sleep(0.1)

                    if self.__timed_exposure_handle.is_in_progress():
                        print("Timed exposure did not complete within 10 seconds, something may have gone wrong.")
                        self.__logger.log("Timed exposure did not complete within 10 seconds, something may have gone wrong.", level="ERROR", l_type="EXP", subsystem="Oscilloscope")
                        self.__timed_exposure_handle.abort()
                        self.__timed_exposure_handle = None
                        continue

                    start_t = time.monotonic()
                    while not self.__did_read and (time.monotonic() - start_t) < 10.0:
                        time.sleep(0.1)

                    if not self.__did_read:
                        print("Timed exposure completed but no oscilloscope data was read, something may have gone wrong.")
                        self.__logger.log("Timed exposure completed but no oscilloscope data was read, something may have gone wrong.", level="ERROR", l_type="EXP", subsystem="Oscilloscope")

                    print("Step exposure completed.")
                    print(f"Step result: {self.__timed_exposure_handle.get_result(uuids.UUID_LASER_CONTROLLER)}")

                    self.__timed_exposure_handle = None
                finally:
                    self.__doing_step_exposure = False


    def __thread(self, stop_flag: StopFlag):
        self.__exp_reader = ExperimentReader(os.path.join(os.environ["EUVL_PATH"], "datasets"), "exposure")
        self.__data_reader = DataReader(os.path.join(os.environ["EUVL_PATH"], "datasets"))

        while stop_flag.run():
            time.sleep(0.1)

            if self.__start_handle is not None:
                self._on_did_start()
                self.__start_handle = None

            if self.__stop_handle is not None:
                self._on_did_stop()
                self.__stop_handle = None

            if self.__preinit_handle is not None:
                self._on_did_preinit()
                self.__preinit_handle = None

            #if self.__exp_id is not None and self._has_timed_out():
            #    print("Exposure has timed out, resetting state.")
            #    self.__stop_exp()

            self.__update_laser_status()
            self.__update_status_items()

            if not self.__osc_queue.empty() and not self.__exp_id is None:
                self.__did_read = True
                #print("New oscilloscope data available, writing...")
                start, end, data, indexes, uid = self.__osc_queue.get()
                is_step_exposure = self.__doing_step_exposure
                exposure_start_ns = self.__get_exposure_start_ns()

                if start < self.__last_laser_off_time <= end:
                    print(f"[OscilloscopeSubsystem] Skipped snapshot {uid}: laser off during segment ({start}..{end}, off at {self.__last_laser_off_time}).")
                    self.__logger.log(
                        f"Skipping snapshot {uid}: laser off detected during segment.",
                        level="WARNING",
                        l_type="EXP",
                        subsystem="Oscilloscope",
                    )
                    continue
                
                for i in range(5):
                    try:
                        ok = self.__writer.write_wf(
                            start,
                            end,
                            data,
                            indexes,
                            uid,
                            is_step_exposure=is_step_exposure,
                            exposure_start_ns=exposure_start_ns,
                        )
                        if ok:
                            break
                        print(f"Failed to write waveform data for snapshot {uid}, attempt {i+1}. Retrying...")
                        self.__log(f"Failed to write waveform data for snapshot {uid}, attempt {i+1}. Retrying...", level="WARNING", l_type="EXP", subsystem="Oscilloscope")
                    except Exception as e:
                        print(f"Error writing waveform data for snapshot {uid}, attempt {i+1}: {e}")
                        self.__logger.log(f"Error writing waveform data for snapshot {uid}, attempt {i+1}: {e}", level="ERROR", l_type="EXP", subsystem="Oscilloscope")
                        time.sleep(0.5)

                analysis = analyze_snapshot(
                    start,
                    end,
                    data,
                    indexes,
                    is_step_exposure=is_step_exposure,
                    exposure_start_ns=exposure_start_ns,
                )
                with self.__run_state_lock:
                    self.__current_dose += analysis.total_dose_mj_cm2
                    self.__current_time += analysis.runtime_contribution_seconds
                    current_dose = self.__current_dose
                    current_time = self.__current_time
                    target_dose = self.__target_dose
                    target_time = self.__target_time

                self.__dose_publisher.value = current_dose
                self.__time_publisher.value = current_time



                self.__logger.log(f"Saved snapshot {uid}", level="DEBUG", l_type="EXP", subsystem="Oscilloscope")
                self.__logger.log(f"Current dose: {current_dose} mJ/cm2, time: {current_time}", level="DEBUG", l_type="EXP", subsystem="Oscilloscope")
                
                if self.__do_run and target_dose is not None and current_dose >= target_dose:
                    print(f"Target dose of {target_dose} mJ/cm2 reached, stopping exposure.")
                    self.__logger.log(f"Target dose of {target_dose} mJ/cm2 reached, stopping exposure.", level="INFO", l_type="EXP", subsystem="Oscilloscope")
                    self.__stop_experiment_event_sender.call((f"Target dose of {target_dose} mJ/cm2 reached").encode("utf-8"), [])

                if self.__do_run and target_time is not None and current_time >= target_time:
                    print(f"Target time of {target_time} s reached, stopping exposure.")
                    self.__logger.log(f"Target time of {target_time} s reached, stopping exposure.", level="INFO", l_type="EXP", subsystem="Oscilloscope")
                    self.__stop_experiment_event_sender.call((f"Target time of {target_time} s reached").encode("utf-8"), [])

                if not ok:
                    print("Failed to write oscilloscope data")
                    self.__logger.log("Failed to write oscilloscope data", level="ERROR", l_type="EXP", subsystem="Oscilloscope")
                    #self.osc.set_active(False)
                else:
                    print(f"[OscilloscopeSubsystem] Saved snapshot {uid}: segment window {start}..{end}.")

                    self.__on_new_segment_publisher.value = segment_bytes.encode([self.__exp_id.bytes, uid.bytes])

            if self.__finalizing_exp_id is not None and self.osc.is_idle() and self.__osc_queue.empty():
                if not self.__writer.flush():
                    self.__log("Timed out waiting for queued waveform writes before final dose calculation.", level="ERROR")
                    continue

                exp = self.__finalizing_exp_id
                self.__finalizing_exp_id = None
                self.__writer.set_exp_id(None)
                with self.__run_state_lock:
                    if self.__exp_id == exp:
                        self.__exp_id = None
                self.__dose_queue.put(exp)

            if not self.__dose_queue.empty():
                exp = self.__dose_queue.get()
                rec = None
                for _ in range(5): # retry a few times to get the experiment record, in case it's not immediately available
                    try:
                        rec = self.__exp_reader.get_run(exp)
                        break
                    except ValueError:
                        print(f"Experiment record for {exp} not found yet, retrying...")
                        time.sleep(0.5)
                    except AttributeError:
                        print(f"Experiment was not saved properly, ignoring this dose calculation.")
                        break
                
                if rec is None:
                    print(f"Experiment record for {exp} not found after retries, skipping dose calculation.")
                    continue
                
                try:
                    dose, runtime = calculate_dose_of_experiment(exp, self.__data_reader)
                    rec.add_tag("runtime", runtime)
                    rec.add_tag("dose", dose)
                    self.__logger.log(f"Saved dose for experiment {exp}: {dose} mJ/cm2", level="INFO", l_type="EXP", subsystem="Oscilloscope")

                except Exception as e:
                    self.__logger.log(f"Error calculating dose for experiment {exp}: {e}", level="ERROR", l_type="EXP", subsystem="Oscilloscope")
                    continue

    def __on_got_subsystem(self, sh: client._RegisteredSubsystemHandle):
        print("Registered subsystem with UUID: ", sh.get_info().get_uuid())
        self.__subsystem = sh

        self.__on_new_segment_publisher = sh.get_kv_property(b"new_segment", False, True, True)
        self.__dose_publisher = sh.get_kv_property(b"cur_dose", False, True, True)
        self.__time_publisher = sh.get_kv_property(b"cur_time", False, True, True)

        self.__dose_publisher.set_type(types.FloatTypeSpecifier())
        self.__time_publisher.set_type(types.FloatTypeSpecifier())

        self.__exposure_state_kv = sh.add_remote_kv(
            uuids.UUID_EXPOSURE_CONTROLLER,
            dds_subsystem.KVDescriptor(types.ByteTypeSpecifier(), b"experiment_state", True, True, False),
        )
        self.__exposure_state_kv.on_new_data_received(self.__on_exposure_state)

        self.__stop_experiment_event_sender = sh.add_event_provider(f"stop_exposure".encode("utf-8"))
        self.__do_timed_exposure_event = sh.add_event_provider(b"laser_do_timed_exposure")

        self._setup_subsystem(sh)

    def _can_start(self, settings: ExposureSettings, state: RunState) -> tuple[bool, bytes]:
        print("Started exposure with UUID: ", state.get_uuid())

        if state.get_settings().get_attr("target_dose") > 0.1:
            target_dose = state.get_settings().get_attr("target_dose")
            print(f"Target dose for this exposure: {target_dose} mJ/cm2")
        else:
            target_dose = None

        if state.get_settings().get_attr("target_time") > 0.1:
            target_time = state.get_settings().get_attr("target_time")
            print(f"Target time for this exposure: {target_time} s")
        else:
            target_time = None

        if target_dose is not None and target_time is not None:
            self.__logger.log("Warning: both target dose and target time are set. Refusing to start exposure.", level="WARNING", l_type="EXP", subsystem="Oscilloscope")
            return False, b"Cannot set both target dose and target time. Please set only one of them."

        is_new_run = False
        with self.__run_state_lock:
            if self.__exp_id == state.get_uuid():
                if target_dose is not None and self.__current_dose >= target_dose:
                    return False, b"Measured dose reached the target before the exposure phase opened."
            else:
                is_new_run = True
                self.__exp_id = state.get_uuid()
                self.__current_dose = 0.0
                self.__current_time = 0.0
                self.__target_dose = target_dose
                self.__target_time = target_time
                self.__exposure_start_ns = None

        if is_new_run:
            self.__writer.set_exp_id(self.__exp_id)

        return super()._can_start(settings, state)
    
    def _on_preinit(self, handle):
        self.__preinit_handle = handle
        self.__last_laser_on = False
        self.__last_laser_off_time = 0

        self.osc.set_active(True)

        return super()._on_preinit(handle)

    def _on_start(self, handle: client._EventHandler._IncomingEventHandle) -> bytes:
        self.__start_handle = handle

        self.__logger.log("Scope starting recording.", level="INFO", l_type="EXP", subsystem="Oscilloscope")

        self.__do_run = True

        return b"Scope started recording."
    
    def _on_stop(self, handle: client._EventHandler._IncomingEventHandle) -> bytes:
        self.__stop_handle = handle

        self.__logger.log("Scope stopping recording.", level="INFO", l_type="EXP", subsystem="Oscilloscope")

        self.__stop_exp()
        self.__do_run = False

        return b"Scope stopped recording."
    
    def __stop_exp(self):
        self.osc.set_active(False)
        with self.__run_state_lock:
            self.__finalizing_exp_id = self.__exp_id
    
    def _on_continue_state(self):
        if self.__exp_id is not None:
            return True, b"Scope is recording"
        
        return False, b"Scope is idle"
    
    def close(self):
        self.__daemon.stop()
        self.__writer.close()


    def ok(self):
        return self.__daemon.is_ok() and self.osc.ok()


def main(stop_event):
    osc = ScopeReader("TCPIP0::10.11.13.220::5025::SOCKET")
    #osc = DummyOscilloscope()
    subsystem = OscilloscopeSubsystem(osc)
    #subsystem.STEP_DOSE_TGT = -1.0 # sim laser does not support this
    print("Oscilloscope subsystem initializing...")
    #proc = RealtimeDoseCalc(osc)

    try:
        osc.start()
        time.sleep(1) # wait for oscilloscope to be ready
        while subsystem.ok() and not stop_event.is_set():
            time.sleep(0.1)

    except KeyboardInterrupt:
        pass
    finally:
        print("Shutting down oscilloscope subsystem...")
        osc.close()
        subsystem.close()

def test_main():
    osc = ScopeReader("TCPIP0::10.11.13.220::5025::SOCKET")
    osc.start()
    osc.set_active(True)

if __name__ == "__main__":
    test_main()
