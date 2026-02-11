import json
import random
import time, struct, os, signal, re, sys, threading, numpy as np
import uuid
from datetime import date
from pyvisa import ResourceManager, errors as visa_errors
import socket
import csv
import queue
import math
import tkinter as tk
import numpy as np
import matplotlib
import segment_bytes
matplotlib.use("TkAgg")

from matplotlib.figure import Figure
from matplotlib import colors as mpl_colors
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.ticker import FuncFormatter

from scipy import signal
from datetime import datetime

from ipi_ecs.core.daemon import StopFlag, Daemon
from ipi_ecs.logging.client import LogClient

import ipi_ecs.core.tcp as tcp
import ipi_ecs.dds.client as client
import ipi_ecs.subsystems.experiment_client as exp_client
from ipi_ecs.subsystems.experiment_controller import ExperimentReader, RunRecord, RunState

import chamber_ctl.subsystems.uuids as uuids
from chamber_ctl.subsystems.exposure_controller import ExposureSettings

class OscilloscopeStream:
    def start(self):
        pass
       
    def close(self):
        pass

    def ok(self):
        return False
    
    def is_capturing(self):
        return False
    
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
        self.__state = "IDLE"
        self.__last_capturing = 0

        self.__do_capture = False

        self.__out_queue = queue.Queue() # for processed data

        self.__daemon = Daemon()
        self.__daemon.add(self.__proc_thread)
        self.__daemon.add(self.__read_thread)

    def start(self):
        self.configure()
        self.__daemon.start()

    def set_active(self, active):
        self.__do_capture = active

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

        self.__state = "IDLE"
        self.__is_capturing = False
        # Freeze a consistent snapshot is already ensured (we're stopped)
        self.scope.write(":WAVeform:SEQuence 0,1")         # or 0,<next_start> in your loop
        self.scope.write(":WAVeform:SOURce C1")
        self.scope.write(":WAVeform:PREamble?")
        desc = self.read_hash_block(self.scope)                 # returns WAVEDESC payload

        self.scope.write(":WAVeform:DATA?")
        data = self.read_hash_block(self.scope)                 # returns concatenated frame data

        self.__read_queue.put((self.last_start_cmd, desc, data, uuid.uuid4()))

    def __proc_thread(self, stop_flag: StopFlag):
        while stop_flag.run():
            start, desc, data, uid = self.__read_queue.get() # wait for signal to read

            # 3) Decode to time + voltages
            t, V, meta, timestamps = self.decode_sequence_waveforms(desc, data, 10)
            
            if len(V) == 0:
                continue

            # print(f"Got {len(V)} frames, {len(V[0])} pts/frame. First timestamp: {timestamps[0]} ns. Meta: {meta}")

            indexes = []
            data = np.empty((0, 2))
            cur_time = 0
            for nv in range(0, len(V), int(self.NUM_SKIP)):
                myrealtime = np.copy(timestamps[nv])
                myrealtime += self.start_epoch

                indexes.append((len(data), float(myrealtime) / 1000000000.0))

                values = np.column_stack((t[nv], V[nv]))
                data = np.vstack((data, values))

            self.__out_queue.put((start, data, indexes, uid))

    def __read_thread(self, stop_flag: StopFlag):
        was_running = False
        while stop_flag.run():
            if not self.__do_capture:
                if was_running:
                    print("Capture deactivated, stopping any ongoing capture...")
                    self.scope.write(":STOP")
                    self.__state = "IDLE"
                    self.__is_capturing = False
                    was_running = False

                time.sleep(0.1)
                continue
            
            was_running = True
            self.capture_burst_and_read(100)
       
    def close(self):
        self.scope.write(":ACQ:SEQuence OFF")
        self.scope.write(":STOP")
        self.scope.close()

        self.__daemon.stop()

    def ok(self):
        return self.__daemon.is_ok()
    
    def is_capturing(self):
        return self.__is_capturing
    
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

class DummyOscilloscope(OscilloscopeStream):
    def __init__(self):
        self.__is_capturing = False
        self.__state = "IDLE"
        self.__last_capturing = 0

        self.__out_queue = queue.Queue() # for processed data

        self.__do_capture = False

        self.__daemon = Daemon()
        self.__daemon.add(self.__proc_thread)

    def start(self):
        self.__daemon.start()

    def set_active(self, active):
        print(f"Setting dummy oscilloscope active: {active}")
        self.__do_capture = active

    def dummy_wf(self):
        timestamp = time.time_ns()
        t = np.linspace(0, 10e-6, num=1000)
        t_square = np.linspace(2*math.pi * 0.5, 2*math.pi * 1.5, num=1000)

        # Add some random offsets to simulate noise and timing jitter
        rand_off_t = random.uniform(-0.0625 * math.pi, 0.06125 * math.pi)
        rand_off_v = random.uniform(-0.1, 0.1)

        t_square += rand_off_t
        V = ((signal.square(t_square, 0.1) + 1.0) / 2.0) * 0.4
        V += rand_off_v

        return t, V, timestamp
    
    def dummy_wfs(self, num):
        ts, Vs, timestamps = [], [], []
        for i in range(num):
            time.sleep(0.01)

            t, V, timestamp = self.dummy_wf()
            ts.append(t)
            Vs.append(V)
            timestamps.append(timestamp)

        return ts, Vs, dict(), timestamps, uuid.uuid4()


    def __proc_thread(self, stop_flag: StopFlag):
        while stop_flag.run():
            if not self.__do_capture:
                time.sleep(0.1)
                continue

            start = time.time_ns()
            self.last_start_cmd = time.time_ns()

            self.__is_capturing = True
            self.__state = "CAPTURING"
            print("Dummy oscilloscope capturing...")
            t, V, meta, timestamps, uid = self.dummy_wfs(100)
            print("Dummy oscilloscope finished capturing.")

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

            print("Dummy oscilloscope generated new data, writing...")
            self.__out_queue.put((start, data, indexes, uid))
       
    def close(self):
        self.__daemon.stop()

    def ok(self):
        return self.__daemon.is_ok()
    
    def is_capturing(self):
        return self.__is_capturing
    
    def get_state(self):
        return self.__state
    
    def get_last_capture_time(self):
        return self.__last_capturing
    
    def get_out_queue(self):
        return self.__out_queue
    
    def get_last_start_cmd_time(self):
        return self.last_start_cmd / 1e9
    
class ScopeWriter:
    def __init__(self):
        self.__SAVE_PATH = os.path.join(os.environ["EUVL_PATH"], "datasets")

        self.__exp_reader = None
        self.__record = None
        self.__s_uuid = None

        self.__write_queue = queue.Queue()

        self.__daemon = Daemon()
        self.__daemon.add(self.__writer_thread)
        self.__daemon.start()

    def __writer_thread(self, stop_flag: StopFlag):
        self.__exp_reader = ExperimentReader(self.__SAVE_PATH, "exposure")

        while stop_flag.run():
            if self.__record is None and self.__s_uuid is not None:
                self.__do_get_record()

            if not self.__write_queue.empty():
                start, data, indexes, uid = self.__write_queue.get()
                self.__write_wf(start, data, indexes, uid)

            time.sleep(0.1)

    def set_exp_id(self, s_uuid):
        self.__s_uuid = s_uuid
        self.__record = None

    def __do_get_record(self):
        self.__record = self.__exp_reader.get_run(self.__s_uuid)

    def write_wf(self, start, data: np.ndarray, indexes, uid):
        if self.__record is None:
            print("No record available for writing, skipping...")
            return
        
        self.__write_queue.put((start, data, indexes, uid))
    
    def __write_wf(self, start, data: np.ndarray, indexes, uid):
        if self.__record is None:
            return
        
        file = self.__record.get_record().resource(f"snap_{str(uid)}.npz", "snapshot", "wb")
        np.savez(file, data=data, indexes=indexes)
        file.close()

        file_meta = self.__record.get_record().resource(f"snap_{str(uid)}.json", "snap_meta", "w")
        meta = {
            "start": start,
            "num_points": len(data),
            "num_frames": len(indexes),
        }
        json.dump(meta, file_meta)
        file_meta.close()

    def close(self):
        self.__daemon.stop()

class DataReader:
    def __init__(self, path):
        self.path = path
        self.__exp_reader = ExperimentReader(path, "exposure")

    def get_snapshots(self, e_uuid):
        record = self.__exp_reader.get_run(e_uuid)
        resources = record.get_record().list_resources()

        snapshots = dict()

        for fname, f_type in resources:
            if f_type == "snapshot" and fname.endswith(".npz"):
                uid_str = fname[len("snap_"):-len(".npz")]
                try:
                    uid = uuid.UUID(uid_str)
                except ValueError:
                    print(f"Invalid UUID in filename: {fname}, skipping...")
                    continue

                snapshots[uid] = (record.get_record().resource(fname, "snapshot", "rb"), record.get_record().resource(f"snap_{uid}.json", "snap_meta", "r"))

class OscilloscopeSubsystem(exp_client.ExperimentClient):
    def __init__(self, scope: OscilloscopeStream):
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
        self.__subsystem = None

        self.__exp_id = None

        self.__preinit_handle = None
        self.__start_handle = None
        self.__stop_handle = None

        self.__writer = ScopeWriter()

        def _on_ready():
            if self.__did_config:
                return
            
            self.__did_config = True
            sh = self.__client.register_subsystem("Oscilloscope Controller", uuids.UUID_OSCILLOSCOPE_CONTROLLER)

            self.__on_got_subsystem(sh)

        #print("Registering subsystem...")
        self.__client = client.DDSClient(c_uuid, logger=self.__logger)
        self.__client.when_ready().then(_on_ready)

        super().__init__("exposure", "Oscilloscope", self.__logger)
        self.register_experiment_settings_type(ExposureSettings)

        self.__daemon = Daemon()
        self.__daemon.add(self.__thread)
        self.__daemon.start()

    def __thread(self, stop_flag: StopFlag):
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

            if not self.__osc_queue.empty():
                print("New oscilloscope data available, writing...")
                start, data, indexes, uid = self.__osc_queue.get()
                self.__writer.write_wf(start, data, indexes, uid)

                self.__on_new_segment_publisher.value = segment_bytes.encode([self.__exp_id.bytes, uid.bytes])


    def __on_got_subsystem(self, sh: client._RegisteredSubsystemHandle):
        print("Registered subsystem with UUID: ", sh.get_info().get_uuid())
        self.__subsystem = sh

        self.__on_new_segment_publisher = sh.get_kv_property(b"new_segment", False, True, True)

        self._setup_subsystem(sh)

    def _can_start(self, settings: ExposureSettings, state: RunState) -> tuple[bool, bytes]:
        print("Started exposure with UUID: ", state.get_uuid())
        self.__exp_id = state.get_uuid()

        self.__writer.set_exp_id(self.__exp_id)

        return super()._can_start(settings, state)
    
    def _on_preinit(self, handle):
        self.__preinit_handle = handle
        return super()._on_preinit(handle)

    def _on_start(self, handle: client._EventHandler._IncomingEventHandle) -> bytes:
        self.__start_handle = handle

        self.__logger.log("Scope starting recording.", level="INFO", l_type="EXP", subsystem="Oscilloscope")

        self.osc.set_active(True)

        return b"Scope started recording."
    
    def _on_stop(self, handle: client._EventHandler._IncomingEventHandle) -> bytes:
        self.__stop_handle = handle

        self.__logger.log("Scope stopping recording.", level="INFO", l_type="EXP", subsystem="Oscilloscope")

        self.osc.set_active(False)

        return b"Scope stopped recording."
    
    def _on_continue_state(self):
        if self.__exp_id is not None:
            return True, "Scope is recording"
        
        return False, "Scope is idle"
    
    def close(self):
        self.__daemon.stop()
        self.__writer.close()


def main():
    osc = DummyOscilloscope() #ScopeReader("TCPIP0::10.11.13.220::5025::SOCKET")
    subsystem = OscilloscopeSubsystem(osc)
    #proc = RealtimeDoseCalc(osc)

    try:
        osc.start()
        while osc.ok():
            time.sleep(0.1)

    except KeyboardInterrupt:
        pass
    finally:
        osc.close()
        subsystem.close()

if __name__ == "__main__":
    main()
