from math import floor
import multiprocessing
import os
import pickle
import queue
import struct
import time
import traceback
import uuid
import sys
import mt_events
import segment_bytes

from enum import Enum

from ipi_ecs.core import daemon
from ipi_ecs.dds.magics import *
import ipi_ecs.dds.subsystem as subsystem
import ipi_ecs.dds.types as types
import ipi_ecs.dds.client as client
import ipi_ecs.dds.magics as magics
import ipi_ecs.core.tcp as tcp
import ipi_ecs.db.db_library as db_library

from ipi_ecs.logging.client import LogClient
from ipi_ecs.subsystems.experiment_client import ExperimentClient, RunState

from chamber_ctl.subsystems import uuids
from chamber_ctl.subsystems.target_motion import TargetMotion, TargetMotionConfig, TargetMotionProfile, MotionSegment, MotionState
from chamber_ctl.subsystems.ljs_target_motion import LJSerialTargetMotion
from chamber_ctl.subsystems.exposure_controller import ExposureSettings


class MockTargetMotion(TargetMotion):
    def __init__(self, config: TargetMotionConfig = None):
        self.__current_l = 0.0
        self.__current_r = 0.0

        self.__target_l = 0.0
        self.__target_r = 0.0

        self.__l_speed = 0.0
        self.__r_speed = 0.0

        self.__jog_l = 0.0
        self.__jog_r = 0.0

        self.__homing = False

        self.__config = config

        self.__daemon = daemon.Daemon()
        self.__daemon.add(target=self.__motion_thread)

    def start(self):
        self.__daemon.start()

    def close(self):
        self.__daemon.stop()

    def set_raw_position(self, l_pos, r_pos):
        self.__current_l = l_pos
        self.__current_r = r_pos

        return True

    def __motion_thread(self, stop_flag: daemon.StopFlag):
        last_t = time.monotonic()
        last_print_t = last_t
        while stop_flag.run():
            time.sleep(0.1)

            cur_t = time.monotonic()
            dt = cur_t - last_t
            last_t = cur_t

            if self.__jog_l != 0.0:
                self.__target_l += self.__jog_l * dt

            if self.__jog_r != 0.0:
                self.__target_r += self.__jog_r * dt

            if self.__homing:
                self.__target_l = 0.0
                self.__target_r = 0.0

                self.__l_speed = self.__config.traverse_speed_l
                self.__r_speed = self.__config.traverse_speed_r

                if self.__current_l == 0.0 and self.__current_r == 0.0:
                    self.__homing = False

            if self.__current_l < self.__target_l:
                self.__current_l += self.__l_speed * dt
                if self.__current_l > self.__target_l:
                    self.__current_l = self.__target_l
            elif self.__current_l > self.__target_l:
                self.__current_l -= self.__l_speed * dt
                if self.__current_l < self.__target_l:
                    self.__current_l = self.__target_l

            if self.__current_r < self.__target_r:
                self.__current_r += self.__r_speed * dt
                if self.__current_r > self.__target_r:
                    self.__current_r = self.__target_r
            elif self.__current_r > self.__target_r:
                self.__current_r -= self.__r_speed * dt
                if self.__current_r < self.__target_r:
                    self.__current_r = self.__target_r

            if cur_t - last_print_t > 0.025:
                last_print_t = cur_t
                #print(f"MockTargetMotion @ L={self.__current_l:.3f}/{self.__target_l:.3f} R={self.__current_r:.3f}/{self.__target_r:.3f}")

    def move_to_position(self, l_pos: float, r_pos: float, speed_l: float, speed_r: float):
        self.__target_l = l_pos
        self.__target_r = r_pos

        self.__l_speed = speed_l
        self.__r_speed = speed_r

        self.__jog_l = 0.0
        self.__jog_r = 0.0

    def get_position(self):
        return self.__current_l, self.__current_r
    
    def get_target_position(self):
        return self.__target_l, self.__target_r
    
    def is_moving(self):
        return not (self.__current_l == self.__target_l and self.__current_r == self.__target_r)
    
    def jog(self, delta_l: float, delta_r: float):
        self.__jog_l = delta_l
        self.__jog_r = delta_r

        self.__l_speed = abs(self.__jog_l)
        self.__r_speed = abs(self.__jog_r)

    def is_jogging(self):
        return not (self.__jog_l == 0.0 and self.__jog_r == 0.0)
    
    def ready_for_move(self):
        return not self.is_homing()
    
    def home(self):
        self.__homing = True

    def is_homing(self):
        return self.__homing
    
    def stop(self):
        self.__target_l = self.__current_l
        self.__target_r = self.__current_r
    
class TargetMotionController:
    def __init__(self, logger: LogClient = None):
        self.__config = TargetMotionConfig(max_l_size=300.0)

        self.__state = MotionState()
        self.__motion = LJSerialTargetMotion(self.__config, logger, port="COM3")
        #self.__motion = MockTargetMotion(self.__config)
        self.__start_l = 0.0
        self.__start_r = 0.0
        self.__offset_l = 0.0
        self.__offset_r = 0.0

        self.__jog_speed = (0.0, 0.0)
        self.__is_running = False

        self.__traverse_speed = (2.0, 2.0)

        self.__should_start = False

        self.__motion.start()

        self.__logger = logger

        self.__daemon = daemon.Daemon(exception_handler=self.handle_exception)
        self.__daemon.add(target=self.__move_thread)
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
        
        self.__logger.log(msg, level=level, l_type="SW", subsystem="Target Controller", **data)

    def set_profile(self, profile: TargetMotionProfile):
        self.__state.set_profile(profile)

    def get_profile(self):
        return self.__state.get_profile()

    def set_max_repetitions(self, rep_amount: int):
        self.__state.set_rep_amount(rep_amount)

    def __move_thread(self, stop_flag: daemon.StopFlag):
        while stop_flag.run():
            time.sleep(0.11)

            if self.__is_running:
                self.run_profile()

            elif self.__should_start:
                #print("path start position...")

                l_c, r_c = self.__state.get_current_position()
                #print(f"Path start position: ({l_c:.3f}, {r_c:.3f})")
                eff_start_l, eff_start_r = self.__effective_start()
                t_l, t_r = l_c + eff_start_l, r_c + eff_start_r

                #print(f"Moving to position: ({t_l:.3f}, {t_r:.3f}) from current position: {self.__motion.get_position()}")

                self.__motion.move_to_position(t_l, t_r, self.__traverse_speed[0], self.__traverse_speed[1])

                if not self.__motion.is_moving() and self.at_path_position():
                    print("Starting target motion profile...")
                    self.__should_start = False
                    self.__is_running = True

            elif not self.__should_start and not self.__is_running and self.__motion.is_moving() and self.__jog_speed == (0.0, 0.0):
                print("Stopping motion...")
                self.stop()
            elif self.__jog_speed != (0.0, 0.0):
                self.__motion.jog(self.__jog_speed[0], self.__jog_speed[1])
            
    def run_profile(self):
        t_l, t_r, v_l, v_r = self.__state.get_current_motion_command()
        #print(f"Running profile command: target=({t_l:.3f}, {t_r:.3f}) velocity=({v_l:.3f}, {v_r:.3f})")
        #print(f"Current position: {self.__motion.get_position()[0] - self.__start_l}, {self.__motion.get_position()[1] - self.__start_r}")
        #print(f"Current time: {self.__state.get_current_time():.3f}s / {self.__state.get_time_until_end_of_segment():.3f}s until end of segment")
        
        eff_start_l, eff_start_r = self.__effective_start()
        self.__motion.move_to_position(t_l + eff_start_l, t_r + eff_start_r, v_l, v_r)

        if (abs(t_l - (self.__motion.get_position()[0] - eff_start_l)) > 1e-2 and v_l == 0) or (abs(t_r - (self.__motion.get_position()[1] - eff_start_r)) > 1e-2 and v_r == 0):
            print(f"Warning: motion command with zero velocity but not at target! target=({t_l:.3f}, {t_r:.3f}) current=({self.__motion.get_position()[0] - eff_start_l:.3f}, {self.__motion.get_position()[1] - eff_start_r:.3f})")
            return
            #assert False, "Motion command with zero velocity but not at target!"

        l_pos, r_pos = self.__motion.get_position()
        self.__state.update_position(l_pos - eff_start_l, r_pos - eff_start_r)

        #print(f"Motion Profile @ time={self.__state.get_current_time():.3f}s / {self.__state.get_time_until_end_of_segment()} pos=({l_pos - eff_start_l:.3f}, {r_pos - eff_start_r:.3f})")

        if self.__state.get_current_repetition() > self.__state.get_max_repetitions():
            self.__is_running = False
            print("Completed all repetitions of motion profile. Stopping motion.")

    def is_running(self):
        return self.__is_running
    
    def is_moving(self):
        return self.__motion.is_moving()
    
    def can_modify(self):
        ret = not self.__is_running and not self.__should_start and not self.__motion.is_jogging() and self.__motion.ready_for_move() and not self.__motion.is_moving()
        return ret
    
    def can_jog(self):
        return not self.__is_running and not self.__should_start and self.__motion.ready_for_move()
    
    def can_home(self):
        return not self.__is_running and not self.__should_start and not self.__motion.is_moving()

    def is_moving_to_start(self):
        return self.__should_start
        
    def begin_move_here(self):
        if not self.can_modify():
            return False # Can't modify while running
        
        l_pos, r_pos = self.__motion.get_position()
        self.__start_l = l_pos
        self.__start_r = r_pos
        self.__offset_l = 0.0
        self.__offset_r = 0.0
        self.__state.reset()

        self.__is_running = True

    def set_start_position(self, l_pos: float, r_pos: float):
        if not self.can_modify():
            return False # Can't modify while running
        
        self.__start_l = l_pos
        self.__start_r = r_pos
        self.__offset_l = 0.0
        self.__offset_r = 0.0

        return True
    
    def set_start_position_to_current(self):
        if not self.can_modify():
            return False # Can't modify while running
        
        l_pos, r_pos = self.__motion.get_position()
        self.__start_l = l_pos
        self.__start_r = r_pos
        self.__offset_l = 0.0
        self.__offset_r = 0.0

    def __effective_start(self):
        return self.__start_l + self.__offset_l, self.__start_r + self.__offset_r

    def set_offset_position(self, l_offset: float, r_offset: float):
        if not self.can_modify():
            return False

        self.__offset_l = l_offset
        self.__offset_r = r_offset
        return True

    def set_offset_position_to_current(self):
        if not self.can_modify():
            return False

        l_pos, r_pos = self.__motion.get_position()
        self.__offset_l = l_pos - self.__start_l
        self.__offset_r = r_pos - self.__start_r
        return True

    def clear_offset_position(self):
        return self.set_offset_position(0.0, 0.0)

    def goto_start_position(self):
        if not self.can_modify():
            return False # Can't move while running

        eff_start_l, eff_start_r = self.__effective_start()
        self.__motion.move_to_position(eff_start_l, eff_start_r, self.__traverse_speed[0], self.__traverse_speed[1])

    def at_start_position(self):
        l_pos, r_pos = self.__motion.get_position()
        eff_start_l, eff_start_r = self.__effective_start()
        return abs(l_pos - eff_start_l) < 1e-3 and abs(r_pos - eff_start_r) < 1e-3
    
    def at_path_position(self):
        c_time = self.__state.get_current_time()
        l_c, r_c = self.__state.get_position_at_time(c_time)

        l_pos, r_pos = self.__motion.get_position()
        eff_start_l, eff_start_r = self.__effective_start()
        return abs(l_pos - (l_c + eff_start_l)) < 1e-3 and abs(r_pos - (r_c + eff_start_r)) < 1e-3


    def set_time_position(self, time_pos: float):
        if self.__is_running or self.__should_start:
            return False # Can't modify while running
        
        self.__state.resume_from(time_pos)

    def continue_move(self):
        if not self.can_modify():
            return False # Can't modify while running
        
        self.__should_start = True
        return True

    def stop(self):
        self.__motion.stop()
        self.__is_running = False
        self.__should_start = False

    def get_current_position(self):
        return self.__motion.get_position()
    
    def jog(self, delta_l: float, delta_r: float):
        if not self.can_jog():
            return False # Can't jog while running
        
        #print(f"Jogging with delta_l={delta_l}, delta_r={delta_r}")
        
        self.__jog_speed = (delta_l, delta_r)
        return True
    
    def is_jogging(self):
        return self.__jog_speed != (0.0, 0.0)
    
    def get_time_until_end_of_segment(self):
        return self.__state.get_time_until_end_of_segment()
    
    def get_current_segment(self):
        return self.__state.get_current_segment()
    
    def get_current_time(self):
        return self.__state.get_current_time()

    def close(self):
        self.__daemon.stop()
        self.__motion.close()

    def get_state(self):
        return self.__state
    
    def get_start_position(self):
        return self.__start_l, self.__start_r

    def get_offset_position(self):
        return self.__offset_l, self.__offset_r

    def set_offset(self, l_offset: float, r_offset: float):
        return self.set_offset_position(l_offset, r_offset)

    def get_offset(self):
        return self.get_offset_position()
    
    def set_state(self, state: MotionState):
        self.__state = state

        return True

    def home(self):
        if not self.can_home():
            return False # Can't home while running
        
        self.__motion.home()
        return True
    
    def is_homing(self):
        return self.__motion.is_homing()
    
    def get_config(self):
        return self.__config
    
    def get_motion(self):
        return self.__motion

    
class TargetMotionControllerState:
    def __init__(self, position: tuple[float, float], target_position: tuple[float, float], is_running: bool, is_jogging: bool, is_homing: bool, is_moving_to_start: bool, current_time: float, current_segment: int, start_position: tuple[float, float], offset_position: tuple[float, float] = (0.0, 0.0)):
        self.position = position
        self.target_position = target_position
        self.is_running = is_running
        self.is_jogging = is_jogging
        self.is_homing = is_homing
        self.is_moving_to_start = is_moving_to_start
        self.current_time = current_time
        self.current_segment = current_segment
        self.start_position = start_position
        self.offset_position = offset_position

    def encode(self):
        b_data = pickle.dumps(self)
        return b_data
    
    @staticmethod
    def decode(b_data: bytes) -> 'TargetMotionControllerState':
        state = pickle.loads(b_data)
        if not hasattr(state, "offset_position"):
            state.offset_position = (0.0, 0.0)
        if not hasattr(state, "is_moving_to_start"):
            state.is_moving_to_start = False
        return state
    
    def __str__(self):
        return f"TargetMotionControllerState(position={self.position}, target_position={self.target_position}, is_running={self.is_running}, is_jogging={self.is_jogging}, is_homing={self.is_homing}, is_moving_to_start={self.is_moving_to_start}, current_time={self.current_time}, current_segment={self.current_segment}, start_position={self.start_position}, offset_position={self.offset_position})"


class TargetController(ExperimentClient):
    def __init__(self):
        self.__run = True

        c_uuid = uuid.uuid4()

        self.__logger_sock = tcp.TCPClientSocket()

        self.__logger_sock.connect(("127.0.0.1", 11751))
        self.__logger_sock.start()

        self.__logger = LogClient(self.__logger_sock, origin_uuid=c_uuid)

        self.__did_config = False
        self.__subsystem = None

        self.__profile = None
        
        def _on_ready():
            if self.__did_config:
                return
            
            self.__did_config = True
            sh = self.__client.register_subsystem("Target Controller", uuids.UUID_TARGET_CONTROLLER)

            self.__on_got_subsystem(sh)

        super().__init__("exposure", "Target Controller", self.__logger)
        self.register_experiment_settings_type(ExposureSettings)

        #print("Registering subsystem...")
        self.__client = client.DDSClient(c_uuid, logger=self.__logger)
        self.__client.when_ready().then(_on_ready)

        self.__event_queue = mt_events.EventConsumer()

        self.__SAVE_PATH = os.path.join(os.environ["EUVL_PATH"], "datasets")

        self.__preinit_handle = None
        self.__start_handle = None
        self.__stop_handle = None

        self.__home_handle = None
        self.__jog_value = None

        self.__status_publisher = None
        self.__last_status_update = time.monotonic()

        self.__last_jog_write = 0.0
        self.__exp_name = None
        self.__last_running_state = False
        self.__config_save_interval_s = 30.0
        self.__last_periodic_save = time.monotonic()

        self.__last_preinit_feedback = 0.0

        self.__config_save_queue = queue.Queue()
        self.__config_save_in_progress = False

        self.__motion_controller = TargetMotionController(self.__logger)

        self.__config_daemon = daemon.Daemon()
        self.__config_daemon.add(target=self.__config_saver_thread)
        self.__config_daemon.start()

        self.__load_profile()

        self.__daemon = daemon.Daemon(exception_handler=self.handle_exception)
        self.__daemon.add(target=self.__thread)
        self.__daemon.start()
        
        print("TargetController initialized.")

    def __load_profile(self):
        try:
            ok, payload = self.__request_saver_command("load", wait=True, timeout=5.0)
            if not ok:
                raise RuntimeError(f"Failed to load saved motion state: {payload}")

            b_state, b_start_pos, b_offset_pos, b_rot = self.__normalize_profile_payload(payload)
            start_pos = pickle.loads(b_start_pos)
            offset_pos = pickle.loads(b_offset_pos)
            rot = pickle.loads(b_rot)
            state = MotionState.decode(b_state)

            print("Loaded saved motion state:", state)
            print("Loaded saved start position:", start_pos)
            print("Loaded saved offset position:", offset_pos)
            print("Loaded saved rot actuator position:", rot)

            assert self.__motion_controller.set_start_position(*start_pos)
            assert self.__motion_controller.set_offset_position(*offset_pos)
            assert self.__motion_controller.set_state(state)

            current_l = self.__motion_controller.get_motion().get_position()[0]
            assert self.__motion_controller.get_motion().set_raw_position(current_l, rot)

            self.__profile = self.__motion_controller.get_profile()

            self.__logger.log(f"Resuming state (current/remaining): {self.__motion_controller.get_current_time()}/{self.__motion_controller.get_state().get_remaining_time()}", level="DEBUG", l_type="CTRL", subsystem="Target Controller")
            self.__logger.log(f"Start position @ {self.__motion_controller.get_start_position()}", level="DEBUG", l_type="CTRL", subsystem="Target Controller")
            self.__logger.log(f"Offset position @ {self.__motion_controller.get_offset_position()}", level="DEBUG", l_type="CTRL", subsystem="Target Controller")
            return
        except (RuntimeError, ValueError, IndexError, TypeError, struct.error, pickle.PickleError) as exc:
            self.__logger.log(f"Failed to load target motion state from saver thread: {exc}", level="ERROR", l_type="CTRL", subsystem="Target Controller", event="load_profile")
            raise
        
    def __encode_profile_save_data(self):
        return segment_bytes.encode([
            self.__motion_controller.get_state().encode(),
            pickle.dumps(self.__motion_controller.get_start_position()),
            pickle.dumps(self.__motion_controller.get_offset_position()),
            pickle.dumps(self.__motion_controller.get_motion().get_position()[1]),
        ])

    def __get_or_create_profile_record(self, library: db_library.Library):
        recs = library.query({"name": "Target Motion Controller Save State", "limit": 1})

        if not recs:
            self.__logger.log("Could not find saved state record while saving. Creating new record.", level="WARN", l_type="CTRL", subsystem="Target Controller")
            rec = library.create_entry("Target Motion Controller Save State", "Saves the state of the Target Motion Controller")
        else:
            rec = recs[0]

        return rec

    def __default_profile_payload(self):
        profile = TargetMotionProfile()
        profile.add_segment(MotionSegment(10.0, 0.0, 5, 0.5))
        profile.add_segment(MotionSegment(10.0, 1.0, 5, 0.5))
        profile.add_segment(MotionSegment(00.0, 1.0, 5, 0.5))

        state = MotionState(profile)
        state.set_rep_amount(99)

        return state.encode(), pickle.dumps((0.0, 0.0)), pickle.dumps((0.0, 0.0)), pickle.dumps(0.0)

    def __normalize_profile_payload(self, payload):
        if isinstance(payload, tuple) or isinstance(payload, list):
            decoded = list(payload)
        elif isinstance(payload, bytes):
            try:
                decoded = list(segment_bytes.decode(payload))
            except (ValueError, IndexError, TypeError, struct.error):
                legacy = pickle.loads(payload)
                if not isinstance(legacy, (tuple, list)):
                    raise
                decoded = list(legacy)
        else:
            raise TypeError(f"Unsupported profile payload type: {type(payload)}")

        if len(decoded) == 2:
            b_state, b_start_pos = decoded
            b_offset_pos = pickle.dumps((0.0, 0.0))
            b_rot = pickle.dumps(0.0)
            return b_state, b_start_pos, b_offset_pos, b_rot

        if len(decoded) == 3:
            b_state, b_start_pos, b_offset_pos = decoded[:3]
            b_rot = pickle.dumps(0.0)
            return b_state, b_start_pos, b_offset_pos, b_rot
        
        if len(decoded) == 4:
            b_state, b_start_pos, b_offset_pos, b_rot = decoded[:4]
            return b_state, b_start_pos, b_offset_pos, b_rot

        raise ValueError("Profile payload is missing required state fields.")

    def __load_profile_data(self, library: db_library.Library):
        rec = self.__get_or_create_profile_record(library)

        try:
            res = rec.resource("motion_state.bin", "Motion State", "rb")
            b_data = res.read()
            res.close()

            b_state, b_start_pos, b_offset_pos, b_rot = self.__normalize_profile_payload(b_data)

            MotionState.decode(b_state)
            pickle.loads(b_start_pos)
            pickle.loads(b_offset_pos)
            pickle.loads(b_rot)

            return b_state, b_start_pos, b_offset_pos, b_rot
        except (FileNotFoundError, ValueError, IndexError, TypeError, struct.error, pickle.UnpicklingError):
            self.__logger.log("No saved motion state found, or saved state is not valid. Creating new profile.", level="WARN", l_type="CTRL", subsystem="Target Controller")

            b_state, b_start_pos, b_offset_pos, b_rot = self.__default_profile_payload()
            bdata = segment_bytes.encode([b_state, b_start_pos, b_offset_pos, b_rot])

            res = rec.resource("motion_state.bin", "Motion State", "wb")
            res.write(bdata)
            res.close()

            return b_state, b_start_pos, b_offset_pos, b_rot

    def __save_profile(self, library: db_library.Library, bdata: bytes):
        rec = self.__get_or_create_profile_record(library)

        self.__logger.log(f"Saving state (current/remaining): {self.__motion_controller.get_current_time()}/{self.__motion_controller.get_state().get_remaining_time()}", level="DEBUG", l_type="REC", subsystem="Target Controller")
        self.__logger.log(f"Start position @ {self.__motion_controller.get_start_position()}", level="DEBUG", l_type="REC", subsystem="Target Controller")
        self.__logger.log(f"Offset position @ {self.__motion_controller.get_offset_position()}", level="DEBUG", l_type="REC", subsystem="Target Controller")

        res = rec.resource("motion_state.bin", "Motion State", "wb")
        res.write(bdata)
        res.close()

    def __request_profile_save(self):
        self.__last_periodic_save = time.monotonic()
        self.__request_saver_command("save", payload=self.__encode_profile_save_data())

    def __request_saver_command(self, op: str, payload = None, wait: bool = False, timeout: float = 0.0):
        response_queue = queue.Queue(maxsize=1) if wait else None
        self.__config_save_queue.put((op, payload, response_queue))

        if not wait:
            return True, None

        try:
            return response_queue.get(timeout=timeout)
        except queue.Empty:
            return False, b"Timed out waiting for saver thread response."

    def __config_saver_thread(self, stop_flag: daemon.StopFlag):
        library = db_library.Library(self.__SAVE_PATH)
        try:
            while stop_flag.run() and self.__run:
                try:
                    op, payload, response_queue = self.__config_save_queue.get(timeout=0.2)
                except queue.Empty:
                    continue

                if op == "save":
                    bdata = payload
                    while not self.__config_save_queue.empty():
                        try:
                            n_op, n_payload, n_response_queue = self.__config_save_queue.get_nowait()
                            if n_op == "save":
                                bdata = n_payload
                                continue

                            self.__config_save_queue.put((n_op, n_payload, n_response_queue))
                            break
                        except queue.Empty:
                            break

                    self.__config_save_in_progress = True
                    try:
                        self.__save_profile(library, bdata)
                        if response_queue is not None:
                            response_queue.put((True, None))
                    except Exception as exc:
                        self.__logger.log(
                            f"Failed to save target motion state: {exc}",
                            level="ERROR",
                            l_type="CTRL",
                            subsystem="Target Controller",
                            event="save_profile",
                        )
                        if response_queue is not None:
                            response_queue.put((False, str(exc).encode("utf-8", errors="replace")))
                    finally:
                        self.__config_save_in_progress = False
                    continue

                if op == "load":
                    try:
                        payload = self.__load_profile_data(library)
                        if response_queue is not None:
                            response_queue.put((True, payload))
                    except Exception as exc:
                        self.__logger.log(
                            f"Failed to load target motion state: {exc}",
                            level="ERROR",
                            l_type="CTRL",
                            subsystem="Target Controller",
                            event="load_profile",
                        )
                        if response_queue is not None:
                            response_queue.put((False, str(exc).encode("utf-8", errors="replace")))
                    continue

                if op == "close":
                    try:
                        library.close()
                        if response_queue is not None:
                            response_queue.put((True, None))
                    except Exception as exc:
                        if response_queue is not None:
                            response_queue.put((False, str(exc).encode("utf-8", errors="replace")))
                    return

                if response_queue is not None:
                    response_queue.put((False, b"Unknown saver thread operation."))
        finally:
            try:
                library.close()
            except Exception:
                pass

    def __thread(self, stop_flag: daemon.StopFlag):
        while stop_flag.run() and self.__run:
            e = self.__event_queue.get(timeout=0.5)

            if (time.monotonic() - self.__last_periodic_save) >= self.__config_save_interval_s:
                self.__request_profile_save()

            is_running = self.__motion_controller.is_running()
            if is_running != self.__last_running_state:
                self.__request_profile_save()
                self.__last_running_state = is_running

            if self.__preinit_handle is not None:
                # Continue already handles this
                #if not self.__motion_controller.is_running() and self.__motion_controller.at_path_position():
                #    self.__motion_controller.continue_move()

                if time.time() - self.__last_preinit_feedback > 5.0:
                    self.__preinit_handle.feedback(b"Moving to position...")
                    self.__logger.log("Moving to position...", level="INFO", l_type="CTRL", subsystem="Target Controller", event="motion_start")
                    self.__last_preinit_feedback = time.time()
                
                if self.__motion_controller.is_running():
                    self.__logger.log("Started motion.", level="INFO", l_type="CTRL", subsystem="Target Controller", event="motion_start")
                    self._on_did_preinit(OP_OK + b": motion started successfully.")
                    self.__preinit_handle = None

            if self.__start_handle is not None:
                self._on_did_start(OP_OK + b": motion is running.")
                self.__start_handle = None

            if self.__stop_handle is not None and not self.__motion_controller.is_moving():
                self.__logger.log("Stopped motion.", level="INFO", l_type="CTRL", subsystem="Target Controller", event="motion_stop")
                self._on_did_stop(OP_OK + b": motion stopped successfully.")
                self.__stop_handle = None

            if self.__home_handle is not None:
                if not self.__motion_controller.is_homing():
                    self.__logger.log("Homing complete.", level="INFO", l_type="CTRL", subsystem="Target Controller", event="motion_home_complete")
                    self.__home_handle.ret(OP_OK + b": homing has completed successfully.")
                    self.__home_handle = None

            self.__update_status_items()

            if (time.monotonic() - self.__last_jog_write) > 0.5 and self.__motion_controller.is_jogging():
                self.__motion_controller.jog(0.0, 0.0)

            if (time.monotonic() - self.__last_status_update) > 0.2:
                self.__last_status_update = time.monotonic()
                if self.__status_publisher is not None:
                    current_time = self.__motion_controller.get_current_time()
                    start_l, start_r = self.__motion_controller.get_start_position()
                    offset_l, offset_r = self.__motion_controller.get_offset_position()
                    profile = self.__motion_controller.get_profile()
                    if profile is not None:
                        target_l, target_r = profile.get_position_at_time(current_time)
                    else:
                        target_l, target_r = self.__motion_controller.get_current_position()

                    state = TargetMotionControllerState(
                        position=self.__motion_controller.get_current_position(),
                        target_position=(target_l + start_l + offset_l, target_r + start_r + offset_r),
                        is_running=self.__motion_controller.is_running(),
                        is_jogging=self.__motion_controller.is_jogging(),
                        is_homing=self.__motion_controller.is_homing(),
                        is_moving_to_start=self.__motion_controller.is_moving_to_start(),
                        current_time=current_time,
                        current_segment=self.__motion_controller.get_current_segment(),
                        start_position=(start_l, start_r),
                        offset_position=(offset_l, offset_r),
                    )

                    motion_state = self.__motion_controller.get_state()
                    self.__status_publisher.value = [state.encode(), motion_state.encode()]

    def __update_status_items(self):
        if self.__subsystem is None:
            return
        
        if self.__motion_controller.get_start_position() == (0.0, 0.0):
            if not self.__subsystem.get_status_item_exists(1):
                self.__subsystem.put_status_item(subsystem.StatusItem(subsystem.StatusItem.STATE_INFO, 1, "Start position is not defined"))
        else:
            if self.__subsystem.get_status_item_exists(1):
                self.__subsystem.clear_status_item(1)

    def __can_start_motion(self):
        #if self.__motion_controller.get_start_position() == (0.0, 0.0):
        #    return False, b"Motion start has not been defined"
        
        if not self.__motion_controller.can_modify():
            return False, b"Motion controller is busy or not ready for motion"
        
        return True, b""
    
    def _on_continue_state(self):
        if self.__motion_controller.is_running():
            return True, b""
        else:
            return False, b"Motion controller is not running."
            
    
    def _can_preinit(self, settings: ExposureSettings, state: RunState) -> tuple[bool, bytes]:
        t_len = settings.get_target_time() if settings.get_target_time() else 0.0
        
        if t_len > self.__motion_controller.get_state().get_remaining_time():
            return False, f"Exposure target time {t_len} exceeds remaining motion time {self.__motion_controller.get_state().get_remaining_time()}".encode("utf-8")
        
        # Not sure if we can allow the system to move back to start position by itself or to force operator to do it
        # Will leave this commented out for now
        #if not self.__motion_controller.at_path_position():
        #    return False, "Target motion controller is not at start or path position"

        state, reason = self.__can_start_motion()
        return state, reason

    def _on_preinit(self, handle) -> bytes:
        self.__motion_controller.continue_move()
        self.__preinit_handle = handle

        return b"Continuing motion."
    
    def _on_start(self, handle):
        self.__start_handle = handle
        return super()._on_start(handle)
    
    def _on_stop(self, handle) -> bytes:
        self.__motion_controller.stop()
        self.__stop_handle = handle

        return b"Motion stopping."

    def __on_set_start_position(self, s_uuid, param, handle: client._EventHandler._IncomingEventHandle):
        print("Set start position event called by:", s_uuid, param)

        if not self.__motion_controller.can_modify():
            handle.fail(b"Cannot set start position while motion is running or not ready.")
            return

        l_pos, r_pos = self.__motion_controller.get_current_position()
        self.__motion_controller.set_start_position(l_pos, 0)
        self.__request_profile_save()

        self.__logger.log(f"Set start position to current position @ L={l_pos}, R={r_pos}", level="INFO", l_type="CTRL", subsystem="Target Controller", event="set_start_position")

        handle.ret(OP_OK + b": start position set.")

    def __on_set_offset_here_event(self, s_uuid, param, handle: client._EventHandler._IncomingEventHandle):
        print("Set offset position event called by:", s_uuid, param)

        if not self.__motion_controller.can_modify():
            handle.fail(b"Cannot set offset while motion is running or not ready.")
            return

        ok = self.__motion_controller.set_offset_position_to_current()
        if not ok:
            handle.fail(b"Failed to set offset position.")
            return

        self.__request_profile_save()
        l_off, r_off = self.__motion_controller.get_offset_position()

        self.__logger.log(f"Set offset position to current @ L={l_off}, R={r_off}", level="INFO", l_type="CTRL", subsystem="Target Controller", event="set_offset_position")

        handle.ret(OP_OK + b": offset position set.")

    def __on_clear_offset_event(self, s_uuid, param, handle: client._EventHandler._IncomingEventHandle):
        print("Clear offset event called by:", s_uuid, param)

        if not self.__motion_controller.can_modify():
            handle.fail(b"Cannot clear offset while motion is running or not ready.")
            return

        ok = self.__motion_controller.clear_offset_position()
        if not ok:
            handle.fail(b"Failed to clear offset position.")
            return

        self.__request_profile_save()
        self.__logger.log("Cleared motion offset position.", level="INFO", l_type="CTRL", subsystem="Target Controller", event="clear_offset_position")

        handle.ret(OP_OK + b": offset position cleared.")

    def __on_start_move_event(self, s_uuid, param, handle: client._EventHandler._IncomingEventHandle):
        print("Start move event called by:", s_uuid, param)

        if self.__motion_controller.is_running():
            handle.fail(b"Cannot start motion while motion is already running.")
            return
        
        if not self.__motion_controller.can_modify():
            handle.fail(b"Cannot start motion; controller is not ready.")
            return

        ok = self.__motion_controller.continue_move()
        if not ok:
            handle.fail(b"Failed to begin move - motion controller is not ready.")
            return

        self.__logger.log("Resuming motion.", level="INFO", l_type="CTRL", subsystem="Target Controller", event="begin_move")

        handle.ret(OP_OK + b": resuming motion.")

    def __on_stop_move_event(self, s_uuid, param, handle: client._EventHandler._IncomingEventHandle):
        print("Stop move event called by:", s_uuid, param)

        if (not self.__motion_controller.is_running()) and (not self.__motion_controller.is_moving_to_start()):
            handle.fail(b"Cannot stop motion while motion is not running or is moving to start position.")
            return

        self.__motion_controller.stop()

        self.__logger.log("Stopped motion.", level="INFO", l_type="CTRL", subsystem="Target Controller", event="stop_move")

        handle.ret(OP_OK + b": motion stopped.")

    def __on_reset_move_event(self, s_uuid, param: float, handle: client._EventHandler._IncomingEventHandle):
        print("Reset move event called by:", s_uuid, param)

        if self.__motion_controller.is_running() or self.__motion_controller.is_moving_to_start():
            handle.fail(b"Cannot reset motion while motion is running or is moving to start position.")
            return

        self.__motion_controller.set_time_position(param)
        self.__request_profile_save()

        self.__logger.log(f"Set time position: {param}.", level="INFO", l_type="CTRL", subsystem="Target Controller", event="set_pos", position=param)

        handle.ret(OP_OK + b": position set.")

    def __on_home_event(self, s_uuid, param, handle: client._EventHandler._IncomingEventHandle):
        print("Home event called by:", s_uuid, param)

        ok = self.__motion_controller.home()
        if not ok:
            handle.fail(b"Failed to begin homing - motion controller is not ready.")
            return

        self.__logger.log("Homing target motion controller.", level="INFO", l_type="CTRL", subsystem="Target Controller", event="home")

        handle.feedback(b"Homing started.")
        self.__home_handle = handle

    def __on_got_subsystem(self, handle: client._RegisteredSubsystemHandle):
        self.__subsystem = handle

        handle.add_event_handler(b"start_target_motion").on_called(self.__on_start_move_event)
        handle.add_event_handler(b"stop_target_motion").on_called(self.__on_stop_move_event)
        handle.add_event_handler(b"set_target_start").on_called(self.__on_set_start_position)
        handle.add_event_handler(b"set_target_offset_here").on_called(self.__on_set_offset_here_event)
        handle.add_event_handler(b"clear_target_offset").on_called(self.__on_clear_offset_event)
        handle.add_event_handler(b"home_target").on_called(self.__on_home_event)
        handle.add_event_handler(b"set_target_position").on_called(self.__on_reset_move_event).set_types(types.FloatTypeSpecifier(), types.ByteTypeSpecifier())

        self.__jog_value= handle.get_kv_property(b"jog", True, False, False)
        self.__jog_value.set_type(types.VectorTypeSpecifier(types.FloatTypeSpecifier(), 2))
        self.__jog_value.on_new_data_received(self.__on_jog_write)

        self.__status_publisher= handle.get_kv_property(b"status", False, True, True)
        self.__status_publisher.set_type(types.VectorTypeSpecifier(types.ByteTypeSpecifier(), 2))

        self.__profile_publisher= handle.add_kv_handler(b"profile")
        self.__profile_publisher.on_set(self.__on_profile_write)
        self.__profile_publisher.on_get(self.__on_profile_read)
        self.__profile_publisher.set_type(types.ByteTypeSpecifier())

        self._setup_subsystem(handle)

    def __on_profile_write(self, h, requester, v):
        try:
            if not self.__motion_controller.can_modify():
                return (magics.TRANSOP_STATE_REJ, b"Cannot modify profile while motion is running or not ready.")
            
            b_profile, b_config = segment_bytes.decode(v)
            config = TargetMotionConfig.decode(b_config)
            profile = TargetMotionProfile.decode(b_profile)

            self.__motion_controller.set_profile(profile)
            self.__profile = profile
            self.__request_profile_save()

            self.__logger.log("Target motion profile updated.", level="INFO", l_type="CTRL", subsystem="Target Controller", event="update_profile")
        except (ValueError, pickle.PickleError) as e:
            self.__logger.log(f"Failed to update target motion profile: {e}", level="ERROR", l_type="CTRL", subsystem="Target Controller", event="update_profile")
            return (magics.TRANSOP_STATE_REJ, bytes("Failed to decode profile: " + str(e), "utf-8"))
        
        return (magics.TRANSOP_STATE_OK, OP_OK)

    def __on_profile_read(self, requester):
        if self.__profile is None:
            return (magics.TRANSOP_STATE_REJ, b"No profile loaded.")
        
        return (magics.TRANSOP_STATE_OK, segment_bytes.encode([self.__profile.encode(), self.__motion_controller.get_config().encode()]))

    def __on_jog_write(self, value: list[float]):
        if not self.__motion_controller.can_jog():
            return
        
        self.__last_jog_write = time.monotonic()
        self.__motion_controller.jog(value[0], value[1])

    def ok(self):
        return self.__run and self.__client.ok() and self.__daemon.is_ok()
    
    def close(self):
        print("Closing Target Controller...")

        self.__request_profile_save()

        begin = time.monotonic()
        while (not self.__config_save_queue.empty() or self.__config_save_in_progress) and (time.monotonic() - begin) < 2.0:
            time.sleep(0.05)

        if not self.__config_save_queue.empty() or self.__config_save_in_progress:
            self.__logger.log("Timed out waiting for target motion state save during shutdown.", level="WARN", l_type="CTRL", subsystem="Target Controller", event="save_profile")

        ok, reason = self.__request_saver_command("close", wait=True, timeout=3.0)
        if not ok:
            self.__logger.log(f"Failed to close target motion saver thread cleanly: {reason}", level="WARN", l_type="CTRL", subsystem="Target Controller", event="save_profile")

        self.__motion_controller.close()
        self.__daemon.stop()
        self.__config_daemon.stop()

        self.__client.close()
        self.__logger_sock.close()

        self.__run = False

    def handle_exception(self, e: Exception):
        self.__log("Caught exception on daemon thread!", level="ERROR")
        for line in traceback.format_exception(None, e, e.__traceback__):
            for split in line.split('\n'):
                self.__log(split, level="ERROR")
    
    def __log(self, msg, level = "INFO", **data):
        if self.__logger is None:
            print(level, msg)
            return
        
        self.__logger.log(msg, level=level, l_type="SW", subsystem="Target Controller", **data)


def main(stop_event=None):
    m_target_controller = TargetController()
    print("Target Controller started.")

    try:
        while m_target_controller.ok() and not (stop_event is not None and stop_event.is_set()):
            time.sleep(1)

        print("Target Controller stopping...")
    except KeyboardInterrupt:
        pass
    finally:
        print("Shutting down Target Controller...")
        m_target_controller.close()


if __name__ == "__main__":
    main(None)
