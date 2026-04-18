import argparse
import socket
import threading
import time
import queue
import math
import os
import traceback
import uuid

import ipi_ecs.core.daemon as daemon
import ipi_ecs.core.tcp as tcp
import ipi_ecs.dds.client as client
import ipi_ecs.dds.magics as magics
import ipi_ecs.dds.types as types
from ipi_ecs.subsystems.experiment_client import ExperimentClient, RunState

from ipi_ecs.dds.magics import OP_OK
from ipi_ecs.logging.client import LogClient

from chamber_ctl.subsystems import uuids
from chamber_ctl.subsystems.exposure_controller import ExposureSettings


STEPS_PER_ROT = 1600.0
LIN_LENGTH = 90.0 / (2.54 / STEPS_PER_ROT) # Length / (pitch / steps per revolution)

PI_ADDR = ("10.193.124.226", 11755)
PORT = 11756

STATE_IDLE = 0
STATE_HOMING = 1
STATE_MOVING = 2
STATE_OFFLINE = 3

#print(LIN_LENGTH)

EXPOSURE_OFFSET_Z = 101
EXPOSURE_OFFSET_X = -15

HOME_POS = 80.0
HOME_ANGLE = -2

#SAMPLE_SLOT_ORDER = [11, 4, 10, 3, 0, 5, 9, 2, 1, 6, 8, 7]
SAMPLE_SLOT_ORDER = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
SAMPLE_TH_TOL = 0.1
SAMPLE_Z_TOL = 1.0


def _angle_delta(a: float, b: float) -> float:
    return (a - b + math.pi) % (2.0 * math.pi) - math.pi

def move_r_to_x_y(radius, x, y):
    if y == 0:
        return (0, radius)
    
    #print(y / radius)
    angle = math.asin(y / radius)
    displacement = math.cos(angle) * radius

    return (angle, x - displacement) # target rot angle, lin position

class StepperClient:
    def __init__(self, port, addr):
        self.__sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.__bind_port = port
        self.__server_address = addr
        self.__last_reply = 0
        self.__shutdown = False
        self.__online = False
        self.__last_ack_seq = -1
        self.__next_seq = 0
        self.__enabled = False

        self.__positions = dict()
        self.__moving = dict()
        self.__home = dict()

        self.__command_queue = queue.Queue()

        self.__receiver = threading.Thread(target=self.__receive_thread, daemon=True) 
        self.__receiver.start()

        self.__connection = threading.Thread(target=self.__connection_thread, daemon=True) 
        self.__connection.start()

    def is_shutdown(self):
        return self.__shutdown

    def close(self):
        print("Shutting down client...")
        self.__shutdown = True
        time.sleep(1)

        #self.__connection.join()
        #self.__receiver.join()

        self.__sock.close()

    def __connection_thread(self):
        print("Starting connection thread!")

        while not self.__shutdown:
            self.__send_data()
            time.sleep(0.01)

    def __send_data(self):
        try:
            if not self.__online or time.time() - self.__last_reply > 15:
                if self.__last_reply != 0:
                    print("Timed out!")
                
                self.__sock.sendto(b"REQ_CONN", self.__server_address)
                time.sleep(1)

            elif not self.__command_queue.empty():
                self.__sock.sendto(self.__command_queue.get(), self.__server_address)

        except OSError:
            print("Failed to send data!")
            raise


    def __receive_thread(self):
        print(f"Binding port {self.__bind_port} for incoming connections")
        self.__sock.bind(("0.0.0.0", self.__bind_port))

        while not self.__shutdown:
            self.__receive()

    def __receive(self):
        try:
            data, addr = self.__sock.recvfrom(1024)
            #print(f"Received {data} from {addr}")
            data_str = data.decode("utf-8")

            #print(data_str)
            self.__parse(data_str)
            self.__last_reply = time.time()
            self.__online = True
        except ConnectionResetError:
            if not self.__online:
                pass
            else:
                print("Server appears to be down, failed to connect.")
                self.__online = False
            time.sleep(0.1)

    def __parse(self, data : str):
        blocks = data.strip().split(';')

        for block in blocks:
            if len(block) == 0:
                continue

            tokens = block.split(',')
            b_type = tokens[0]

            #print(b_type)

            if b_type == 'S':
                self.__last_ack_seq = int(tokens[1])
                continue
            elif b_type == 'E':
                self.__enabled = tokens[1] == "True"
                continue

            stepper = int(tokens[1])

            if b_type == 'P':
                self.__positions[stepper] = int(tokens[2])
            elif b_type == 'M':
                self.__moving[stepper] = tokens[2] == "True"
            elif b_type == 'H':
                self.__home[stepper] = int(tokens[2]) if tokens[2] != "None" else None
            

    def __queue_command(self, command, args):
        args_str = ""
        for arg in args:
            args_str += str(arg) + ','

        args_str = args_str.removesuffix(',')
        
        to_send = (f"{self.__next_seq},{command},{args_str}").encode("utf-8")
        self.__command_queue.put(to_send)

        self.__next_seq += 1

    def queue_move(self, stepper, steps):
        self.__queue_command("MOVE", [stepper, int(steps)])

    def queue_set(self, stepper, steps):
        self.__queue_command("SET", [stepper, steps])

    def queue_home(self, stepper, home, speed):
        self.__queue_command("HOME", [stepper, ("T" if home else "F"), int(speed)])

    def get_position(self, stepper):
        return self.__positions[stepper]
    
    def get_home(self, stepper):
        return self.__home[stepper]
    
    def is_moving(self, stepper = None):
        if stepper is None:
            for s in self.__moving.values():
                if s:
                    return True
            return False
        
        return self.__moving[stepper]
    
    def wait_flush(self, timeout = 60):
        start_time = time.time()
        while not self.__command_queue.empty() and (time.time() - start_time) < timeout:
            time.sleep(0.01)

        if (time.time() - start_time) > timeout:
            raise TimeoutError("Timed out")
        
    def wait_ack(self, timeout = 60):
        self.wait_flush(timeout)

        start_time = time.time()
        while self.__last_ack_seq < (self.__next_seq - 1) and (time.time() - start_time) < timeout:
            time.sleep(0.01)

        if (time.time() - start_time) > timeout:
            raise TimeoutError("Timed out")
        
    def is_online(self):
        return self.__online and (time.time() - self.__last_reply < 15)
    
    def is_enabled(self):
        return self.__enabled
    
def calc_target_pose_for_sample_index(sample_index: int, offset = [0, 0], samples = None):
    sample = samples[sample_index]
    th, z = move_r_to_x_y(
        sample['radius'],
        EXPOSURE_OFFSET_Z + offset[0],
        EXPOSURE_OFFSET_X + offset[1],
    )

    th += (sample['angle'] / 360.0) * math.pi * 2
    z = min(z, 89.5)

    return th, z
    
class StageProvider:
    def __init__(self):
        self.__build_sample_data()

    def goto_sample(self, sample, offset = [0, 0]):
        pass
    
    def home(self):
        pass

    def home_rot(self):
        pass

    def move_to(self, th, z):
        pass

    def get_position(self):
        return (0, 0)
    
    def wait_idle(self):
        pass

    def get_state(self):
        return STATE_IDLE
    
    def is_enabled(self):
        return False
    
    def is_at_limit(self):
        return False
    
    def close(self):
        pass

    def get_slot_count(self):
        return len(SAMPLE_SLOT_ORDER)

    def slot_to_sample_index(self, slot: int):
        if slot < 0 or slot >= len(SAMPLE_SLOT_ORDER):
            return None
        return SAMPLE_SLOT_ORDER[slot]

    def calc_target_pose_for_sample_index(self, sample_index: int, offset = [0, 0]):
        return calc_target_pose_for_sample_index(sample_index, offset, self._samples)

    def calc_target_pose_for_slot(self, slot: int, offset = [0, 0]):
        sample_index = self.slot_to_sample_index(slot)
        if sample_index is None:
            raise ValueError(f"Invalid sample slot: {slot}")

        return self.calc_target_pose_for_sample_index(sample_index, offset)

    def __build_sample_data(self):
        self._samples = []
        inner_radius_mm = 0.835 * 25.4
        outer_radius_mm = 1.645 * 25.4

        s_i = 0
        
        #Outer targets (processed first)
        for quadrant in range(4):
            base_angle = 90 * quadrant
            for i, offset in enumerate([21.04, 68.96]):
                angle = base_angle + offset
                self._samples.append({
                    'ring': 2,
                    'position': quadrant * 2 + i,
                    'angle': angle,
                    'label': f"{s_i}",
                    'radius': outer_radius_mm,
                })
                s_i += 1

        #Inner targets (processed second)
        for quadrant in range(4):
            angle = 45 + 90 * quadrant
            self._samples.append({
                'ring': 1,
                'position': quadrant,
                'angle': angle,
                'label': f"{s_i}",
                'radius': inner_radius_mm,
            })
            s_i += 1

class PiStageController(StageProvider):
    def __init__(self, client : StepperClient):
        super().__init__()

        self.__client = client
        self.__opqueue = queue.Queue()
        self.__shutdown_flag = False

        self.__busy = False
        self.__state = STATE_IDLE

        self.__daemon = daemon.Daemon()
        self.__daemon.add(self.__worker)
        self.__daemon.start()

    def __worker(self, stop_flag: daemon.StopFlag):
        while stop_flag.run():
            if not self.__client.is_online():
                self.__state = STATE_OFFLINE
                time.sleep(1)
                continue
            elif self.__state == STATE_OFFLINE:
                self.__state = STATE_IDLE
                

            if not self.__opqueue.empty():
                func, args = self.__opqueue.get()
                func(*args)
                self.__state = STATE_IDLE

            time.sleep(0.1)
            

    def __move_blocking(self, stepper, target, timeout = 1.0):
        target = int(target)
        self.__client.queue_move(stepper, target)
        self.__client.wait_flush()

        start_time = time.time()
        self.__client.wait_ack()
        while not self.__client.is_moving() and (time.time() - start_time) < timeout:
            time.sleep(0.1)

        while self.__client.is_moving():
            time.sleep(0.1)

        start_time = time.time()
        while self.__client.get_position(stepper) != target and (time.time() - start_time) < timeout:
            time.sleep(0.1)

        if (time.time() - start_time) > timeout:
            raise TimeoutError("Timeout while waiting for motion")
        
    def __homing_routine(self):
        self.__state = STATE_HOMING

        self.__home_lin()
        self.__home_rot()

        self.__state = STATE_IDLE

    def __rot_homing_routine(self):
        self.__state = STATE_HOMING

        self.__home_rot()

        self.__state = STATE_IDLE
        
    def __home_lin(self):
        self.__client.queue_move(0, 0)
        self.__client.wait_ack()
        time.sleep(0.5)
        while self.__client.is_moving():
            time.sleep(0.1)

        self.__client.queue_set(0, 0)
        self.__client.queue_set(1, 0)
        self.__client.wait_ack()

        self.__move_blocking(1, LIN_LENGTH * 1.2)
        self.__client.queue_set(1, 0)
        self.__client.queue_move(1, 0)
        self.__client.wait_ack()
       
    def __home_rot(self):
        self.__move_blocking(1, -HOME_POS / (2.54 / STEPS_PER_ROT))
        time.sleep(0.1)

        self.__move_blocking(0, STEPS_PER_ROT * -0.3)

        self.__client.queue_set(0, 0)
        self.__move_blocking(0, STEPS_PER_ROT * 1.2)
        self.__client.queue_set(0, 0)
        self.__client.queue_home(0, True, 400)
        self.__client.queue_move(0, STEPS_PER_ROT * 1.2)
        self.__client.wait_ack()

        time.sleep(0.5)
        while self.__client.is_moving():
            time.sleep(0.1)

        h_pos = self.__client.get_home(0)
        if h_pos is None:
            raise Exception("Could not home rot!")
        time.sleep(1)

        self.__move_blocking(0, STEPS_PER_ROT * 0.9)

        self.__client.queue_home(0, True, 50)
        self.__client.queue_move(0, STEPS_PER_ROT * 1.2)
        self.__client.wait_ack()

        time.sleep(0.5)
        while self.__client.is_moving():
            time.sleep(0.1)

        h_pos = self.__client.get_home(0)
        if h_pos is None:
            raise Exception("Could not home rot!")
        
        time.sleep(0.5)
        self.__client.queue_set(0, 0)
        self.__client.wait_ack()
        self.__move_blocking(0, STEPS_PER_ROT * (HOME_ANGLE / 360.0))
        self.__client.queue_set(0, 0)
        self.__client.queue_move(0, 0)
        self.__client.wait_ack()
        time.sleep(0.5)

    def __shortest_path(self, a2):
        a1 = (-self.__client.get_position(0) / STEPS_PER_ROT) * 2 * math.pi

        a1_clamped = a1 % (2.0 * math.pi)
        a1_clamped += 2.0 * math.pi if a1_clamped < 0 else 0

        nt = a2 + a1 - a1_clamped
        if a2 - a1_clamped > math.pi:
            nt -= 2.0 * math.pi
        if a2 - a1_clamped < -math.pi:
            nt += 2.0 * math.pi

        return nt
    
    def __move(self, th, z):
        self.__state = STATE_MOVING

        self.__client.queue_move(1, -z / (2.54 / STEPS_PER_ROT))
        th1 = self.__shortest_path(th)
        self.__move_blocking(0, (-th1 / (math.pi * 2)) * STEPS_PER_ROT)

        self.__state = STATE_IDLE

    def __rotate_sample_routine(self, sample_index, offset = [0, 0]):
        th, z = self.calc_target_pose_for_sample_index(sample_index, offset)

        self.__move(th, z)

    def goto_sample(self, sample, offset = [0, 0]):
        #if self.__state != STATE_IDLE:
        #    return False
        
        q_item = (self.__rotate_sample_routine, (sample, offset))
        self.__opqueue.put(q_item)
        time.sleep(0.1)
        
    
    def home(self):
        #if self.__state != STATE_IDLE:
        #    return False
        
        q_item = (self.__homing_routine, ())
        self.__opqueue.put(q_item)
        time.sleep(0.1)

    def home_rot(self):
        #if self.__state != STATE_IDLE:
        #    return False
        
        q_item = (self.__rot_homing_routine, ())
        self.__opqueue.put(q_item)
        time.sleep(0.1)

    def move_to(self, th, z):
        #if self.__state != STATE_IDLE:
        #    return False
        
        q_item = (self.__move, (th, z))
        self.__opqueue.put(q_item)
        time.sleep(0.1)

    def get_position(self):
        th = (-self.__client.get_position(0) / STEPS_PER_ROT) * 2 * math.pi
        z = -self.__client.get_position(1) * (2.54 / STEPS_PER_ROT)

        return (th, z)
    
    def wait_idle(self):
        while self.__state != STATE_IDLE:
            time.sleep(0.1)

    def get_state(self):
        return self.__state
    
    def is_enabled(self):
        return self.__client.is_enabled()
    
    def is_at_limit(self):
        return (-self.__client.get_position(1) * (2.54 / STEPS_PER_ROT)) >= 89.45
    
    def close(self):
        self.__daemon.stop()
        self.__client.close()
    

class MockStageController(StageProvider):
    def __init__(self):
        super().__init__()

        self.position = [0, 0]

        self.__target_position = [0, 0]
        self.__state = STATE_IDLE
        

        self.__daemon = daemon.Daemon()
        self.__daemon.add(self.__motion_thread)
        self.__daemon.start()

    def __motion_thread(self, stop_flag: daemon.StopFlag):
        max_th = 0.5 # rad/s
        max_l = 10  # mm/s

        last_t = time.monotonic()
        while stop_flag.run():
            t = time.monotonic()
            dt = t - last_t
            last_t = t

            if abs(self.position[0] - self.__target_position[0]) > 0.01 or abs(self.position[1] - self.__target_position[1]) > 0.01:
                self.__state = STATE_MOVING

                if self.position[0] < self.__target_position[0]:
                    self.position[0] += max_th * dt
                    if self.position[0] > self.__target_position[0]:
                        self.position = [self.__target_position[0], self.position[1]]
                else:
                    self.position[0] -= max_th * dt
                    if self.position[0] < self.__target_position[0]:
                        self.position = [self.__target_position[0], self.position[1]]
                
                if self.position[1] < self.__target_position[1]:
                    self.position[1] += max_l * dt
                    if self.position[1] > self.__target_position[1]:
                        self.position = [self.position[0], self.__target_position[1]]
                else:
                    self.position[1] -= max_l * dt
                    if self.position[1] < self.__target_position[1]:
                        self.position = [self.position[0], self.__target_position[1]]
                
            else:
                self.position = self.__target_position
                self.__state = STATE_IDLE
            
            time.sleep(0.01)

    def goto_sample(self, sample, offset = [0, 0]):
        th, z = self.calc_target_pose_for_sample_index(sample, offset)
        self.__target_position = [th, z]

    def home(self):
        self.position = [0, 0]

    def home_rot(self):
        self.position[1] = 0

    def move_to(self, th, z):
        self.__target_position = [th, z]

    def get_position(self):
        return self.position
    
    def wait_idle(self):
        return

    def get_state(self):
        return STATE_IDLE
    
    def is_enabled(self):
        return self.__state != STATE_IDLE
    
    def close(self):
        self.__daemon.stop()


class SampleMotionSubsystem(ExperimentClient):
    def __init__(self):
        self.__run = True
        self.__connected = False
        self.__did_config = False
        self.__subsystem = None

        self.__offset = [0.0, 0.0]
        self.__target_slot = -1

        self.__position_publisher = None
        self.__offset_kv = None
        self.__sample_kv = None
        self.__enabled_publisher = None
        self.__status_publisher = None

        self.__op_queue = queue.Queue()

        self.__exp_active = False
        self.__exp_selected_slot = None
        self.__preinit_handle = None
        self.__start_handle = None
        self.__stop_handle = None
        self.__pending_stop = False

        c_uuid = uuid.uuid4()
        self.__logger_sock = tcp.TCPClientSocket()
        self.__logger_sock.connect(("127.0.0.1", 11751))
        self.__logger_sock.start()

        self.__logger = LogClient(self.__logger_sock, origin_uuid=c_uuid)

        super().__init__("exposure", "Sample Motion Controller", self.__logger)
        self.register_experiment_settings_type(ExposureSettings)

        #use_mock = os.environ.get("SAMPLE_MOTION_USE_MOCK", "0").strip().lower() in ("1", "true", "yes")
        #if use_mock:
        self.__stage: StageProvider = MockStageController()
        self.__log("Initialized sample motion with mock stage backend.")
        #else:
        #    self.__stage = PiStageController(StepperClient(PORT, PI_ADDR))
        #    self.__log("Initialized sample motion with PI stage backend.")

        self.__client = client.DDSClient(c_uuid, logger=self.__logger)
        self.__client.when_ready().then(self.__on_ready)

        self.__daemon = daemon.Daemon(exception_handler=self.handle_exception)
        self.__daemon.add(self.__worker_thread)
        self.__daemon.add(self.__publisher_thread)
        self.__daemon.start()

    def __log(self, msg, level="INFO", **data):
        if self.__logger is None:
            print(level, msg)
            return
        self.__logger.log(msg, level=level, l_type="SW", subsystem="Sample Motion Controller", **data)

    def handle_exception(self, e: Exception):
        self.__log("Caught exception on daemon thread!", level="ERROR")
        for line in traceback.format_exception(None, e, e.__traceback__):
            for split in line.split('\n'):
                if split:
                    self.__log(split, level="ERROR")

    def __on_ready(self, _=None):
        if self.__did_config:
            return

        self.__did_config = True
        handle = self.__client.register_subsystem("Sample Motion Controller", uuids.UUID_SAMPLE_MOTION_CONTROLLER)
        self.__on_got_subsystem(handle)

    def __on_got_subsystem(self, handle: client._RegisteredSubsystemHandle):
        self.__subsystem = handle

        self.__sample_kv = handle.add_kv_handler(b"sample")
        self.__sample_kv.set_type(types.IntegerTypeSpecifier())
        self.__sample_kv.on_get(self.__on_sample_read)

        self.__position_publisher = handle.get_kv_property(b"position", False, True, True)
        self.__position_publisher.set_type(types.VectorTypeSpecifier(types.FloatTypeSpecifier(), 2))

        self.__offset_kv = handle.add_kv_handler(b"offset")
        self.__offset_kv.set_type(types.VectorTypeSpecifier(types.FloatTypeSpecifier(), 2))
        self.__offset_kv.on_get(self.__on_offset_read)
        self.__offset_kv.on_set(self.__on_offset_write)

        self.__enabled_publisher = handle.get_kv_property(b"enabled", False, True, True)
        self.__enabled_publisher.set_type(types.IntegerTypeSpecifier())

        self.__status_publisher = handle.get_kv_property(b"status", False, True, True)
        self.__status_publisher.set_type(types.IntegerTypeSpecifier())

        handle.add_event_handler(b"goto_sample").on_called(self.__on_goto_sample_event).set_types(
            types.IntegerTypeSpecifier(),
            types.ByteTypeSpecifier(),
        )
        handle.add_event_handler(b"goto").on_called(self.__on_goto_event).set_types(
            types.VectorTypeSpecifier(types.FloatTypeSpecifier(), 2),
            types.ByteTypeSpecifier(),
        )
        handle.add_event_handler(b"home_sample").on_called(self.__on_home_event)
        handle.add_event_handler(b"home_rot_sample").on_called(self.__on_home_rot_event)

        self._setup_subsystem(handle)

        self.__connected = True
        self.__log("Sample Motion DDS endpoints configured.")

    def __parse_selected_slot(self, settings: ExposureSettings):
        raw_sample = settings.get_sample()
        if raw_sample is None:
            return None

        s = str(raw_sample).strip()
        if s == "":
            return None

        try:
            slot = int(s)
        except ValueError:
            return None

        if slot < 0 or slot >= self.__stage.get_slot_count():
            return None

        return slot

    def _can_preinit(self, settings: ExposureSettings, state: RunState) -> tuple[bool, bytes]:
        slot = self.__parse_selected_slot(settings)
        if slot is None:
            return False, b"Experiment sample must be a valid 0-based slot index."

        if self.__stage.get_state() == STATE_OFFLINE:
            return False, b"Sample motion stage is offline."

        self.__exp_selected_slot = slot
        return True, OP_OK

    def _on_preinit(self, handle):
        if self.__exp_selected_slot is None:
            return False, b"No target sample slot selected for preinit."

        self.__exp_active = True
        self.__preinit_handle = handle
        self.__op_queue.put(("exp_preinit", int(self.__exp_selected_slot), handle))
        return True, f": preinit accepted, targeting sample slot {self.__exp_selected_slot}.".encode("utf-8")

    def _on_start(self, handle):
        self.__start_handle = handle
        return True, b": sample motion start acknowledged."

    def _on_stop(self, handle):
        self.__stop_handle = handle
        self.__pending_stop = True
        return True, b": sample motion stop acknowledged."

    def _on_continue_state(self):
        print(f"Continue state check: preinit_handle={self.__preinit_handle}, start_handle={self.__start_handle}, stop_handle={self.__stop_handle}, exp_active={self.__exp_active}, exp_selected_slot={self.__exp_selected_slot}")
        if self.__preinit_handle is not None or self.__start_handle is not None or self.__stop_handle is not None:
            return True, self.EXP_IN_PROGRESS

        if not self.__exp_active or self.__exp_selected_slot is None:
            return False, b"Sample motion experiment state is not active."

        current_slot = self.__resolve_current_slot()
        if current_slot == self.__exp_selected_slot:
            return True, self.EXP_IN_PROGRESS

        return False, f"Sample stage moved off required slot {self.__exp_selected_slot}; current slot is {current_slot}.".encode("utf-8")

    def __fail_preinit(self, reason: bytes):
        self.__exp_active = False
        self.__exp_selected_slot = None
        if self.__preinit_handle is not None:
            self.__preinit_handle.fail(reason)
            self.__preinit_handle = None

    def __resolve_current_slot(self):
        th, z = self.__stage.get_position()

        best_slot = -1
        best_err = None
        for slot in range(self.__stage.get_slot_count()):
            s_th, s_z = self.__stage.calc_target_pose_for_slot(slot, self.__offset)
            th_err = abs(_angle_delta(th, s_th))
            z_err = abs(z - s_z)

            if th_err <= SAMPLE_TH_TOL and z_err <= SAMPLE_Z_TOL:
                err = th_err + z_err
                if best_err is None or err < best_err:
                    best_err = err
                    best_slot = slot

        return best_slot

    def __on_sample_read(self, _requester):
        return (magics.TRANSOP_STATE_OK, self.__resolve_current_slot())

    def __on_offset_read(self, _requester):
        return (magics.TRANSOP_STATE_OK, [float(self.__offset[0]), float(self.__offset[1])])

    def __on_offset_write(self, _h, _requester, value):
        try:
            self.__offset = [float(value[0]), float(value[1])]
        except (TypeError, ValueError, IndexError):
            return (magics.TRANSOP_STATE_REJ, b"Invalid offset payload.")

        if self.__target_slot >= 0:
            sample_index = self.__stage.slot_to_sample_index(self.__target_slot)
            self.__stage.goto_sample(sample_index, self.__offset)

        return (magics.TRANSOP_STATE_OK, OP_OK)

    def __on_goto_sample_event(self, _s_uuid, param: int, handle: client._EventHandler._IncomingEventHandle):
        slot = int(param)
        if self.__stage.slot_to_sample_index(slot) is None:
            handle.fail(b"Invalid sample slot.")
            return

        self.__op_queue.put(("goto_sample", slot, handle))
        handle.feedback(magics.OP_IN_PROGRESS + f": moving to sample slot {slot}.".encode("utf-8"))

    def __on_goto_event(self, _s_uuid, param, handle: client._EventHandler._IncomingEventHandle):
        try:
            th, z = float(param[0]), float(param[1])
        except (TypeError, ValueError, IndexError):
            handle.fail(b"Invalid goto payload.")
            return

        self.__op_queue.put(("goto", (th, z), handle))
        handle.feedback(magics.OP_IN_PROGRESS + b": moving to requested position.")

    def __on_home_event(self, _s_uuid, _param, handle: client._EventHandler._IncomingEventHandle):
        self.__op_queue.put(("home", None, handle))
        handle.feedback(magics.OP_IN_PROGRESS + b": homing stage.")

    def __on_home_rot_event(self, _s_uuid, _param, handle: client._EventHandler._IncomingEventHandle):
        self.__op_queue.put(("home_rot", None, handle))
        handle.feedback(magics.OP_IN_PROGRESS + b": homing rotation.")

    def __wait_for_slot(self, slot: int, timeout: float = 180.0, handle: client._EventHandler._IncomingEventHandle = None, feedback_msg: str = ""):
        begin = time.monotonic()
        last_feedback = 0.0
        while (time.monotonic() - begin) < timeout:
            cur_slot = self.__resolve_current_slot()
            state = self.__stage.get_state()

            if cur_slot == slot and state == STATE_IDLE:
                return True

            if handle is not None and (time.monotonic() - last_feedback) > 5.0:
                remaining = max(0.0, timeout - (time.monotonic() - begin))
                msg = feedback_msg if feedback_msg else f"waiting for sample slot {slot}"
                handle.feedback(magics.OP_IN_PROGRESS + f": {msg} ({remaining:.0f}s remaining).".encode("utf-8"))
                last_feedback = time.monotonic()

            time.sleep(0.1)

        return False

    def __wait_for_idle(self, timeout: float = 180.0, handle: client._EventHandler._IncomingEventHandle = None, feedback_msg: str = ""):
        begin = time.monotonic()
        last_feedback = 0.0
        while (time.monotonic() - begin) < timeout:
            if self.__stage.get_state() == STATE_IDLE:
                return True

            if handle is not None and (time.monotonic() - last_feedback) > 5.0:
                remaining = max(0.0, timeout - (time.monotonic() - begin))
                msg = feedback_msg if feedback_msg else "waiting for stage idle"
                handle.feedback(magics.OP_IN_PROGRESS + f": {msg} ({remaining:.0f}s remaining).".encode("utf-8"))
                last_feedback = time.monotonic()

            time.sleep(0.1)

        return False

    def __worker_thread(self, stop_flag: daemon.StopFlag):
        while stop_flag.run() and self.__run:
            try:
                op, payload, handle = self.__op_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            try:
                if op == "goto_sample":
                    slot = int(payload)
                    sample_index = self.__stage.slot_to_sample_index(slot)
                    self.__target_slot = slot
                    self.__stage.goto_sample(sample_index, self.__offset)

                    if not self.__wait_for_slot(slot, handle=handle, feedback_msg=f"moving to sample slot {slot}"):
                        handle.fail(b"Timed out waiting for target sample.")
                        continue

                    handle.ret(OP_OK + f": reached sample slot {slot}.".encode("utf-8"))

                elif op == "goto":
                    th, z = payload
                    self.__target_slot = -1
                    self.__stage.move_to(th, z)

                    if not self.__wait_for_idle(handle=handle, feedback_msg="moving to requested position"):
                        handle.fail(b"Timed out waiting for target position.")
                        continue

                    handle.ret(OP_OK + b": reached requested position.")

                elif op == "home":
                    self.__stage.home()
                    if not self.__wait_for_idle(handle=handle, feedback_msg="homing stage"):
                        handle.fail(b"Timed out waiting for home completion.")
                        continue
                    self.__target_slot = -1
                    handle.ret(OP_OK + b": home complete.")

                elif op == "home_rot":
                    self.__stage.home_rot()
                    if not self.__wait_for_idle(handle=handle, feedback_msg="homing rotational axis"):
                        handle.fail(b"Timed out waiting for rotational home completion.")
                        continue
                    self.__target_slot = -1
                    handle.ret(OP_OK + b": rotational home complete.")

                elif op == "exp_preinit":
                    slot = int(payload)

                    if self.__stage.slot_to_sample_index(slot) is None:
                        self.__fail_preinit(b"Invalid sample slot in experiment settings.")
                        continue

                    if self.__resolve_current_slot() == slot and self.__stage.get_state() == STATE_IDLE:
                        self.__log(f"Preinit skip: already at sample slot {slot}.", level="INFO", event="preinit_skip")
                        self._on_did_preinit(OP_OK + f": already at sample slot {slot}.".encode("utf-8"))
                        self.__preinit_handle = None
                        continue

                    handle.feedback(magics.OP_IN_PROGRESS + b": preinit homing rotational axis.")
                    self.__stage.home_rot()
                    if not self.__wait_for_idle(handle=handle, feedback_msg="preinit homing rotational axis"):
                        self.__fail_preinit(b"Timed out while homing rotational axis.")
                        continue

                    self.__target_slot = slot
                    sample_index = self.__stage.slot_to_sample_index(slot)
                    handle.feedback(magics.OP_IN_PROGRESS + f": preinit moving to sample slot {slot}.".encode("utf-8"))
                    self.__stage.goto_sample(sample_index, self.__offset)
                    if not self.__wait_for_slot(slot, handle=handle, feedback_msg=f"preinit moving to sample slot {slot}"):
                        self.__fail_preinit(b"Timed out while moving to selected sample slot.")
                        continue

                    self._on_did_preinit(OP_OK + f": sample slot {slot} in position.".encode("utf-8"))
                    self.__preinit_handle = None

            except Exception as exc:
                self.__log(f"Sample motion operation failed: {exc}", level="ERROR")
                if op == "exp_preinit":
                    self.__fail_preinit(str(exc).encode("utf-8", errors="replace"))
                else:
                    handle.fail(str(exc).encode("utf-8", errors="replace"))

    def __publisher_thread(self, stop_flag: daemon.StopFlag):
        while stop_flag.run() and self.__run:
            if self.__enabled_publisher is not None:
                self.__enabled_publisher.value = 1 if self.__stage.is_enabled() else 0

            if self.__status_publisher is not None:
                self.__status_publisher.value = int(self.__stage.get_state())

            if self.__position_publisher is not None:
                th, z = self.__stage.get_position()
                self.__position_publisher.value = [float(th), float(z)]

            if self.__start_handle is not None:
                self._on_did_start(OP_OK + b": sample motion start complete.")
                self.__start_handle = None

            if self.__pending_stop and self.__stop_handle is not None:
                self.__pending_stop = False
                self.__exp_active = False
                self.__exp_selected_slot = None
                self._on_did_stop(OP_OK + b": sample motion experiment state cleared.")
                self.__stop_handle = None

            time.sleep(0.2)

    def ok(self):
        return self.__run and self.__client.ok() and self.__daemon.is_ok()

    def close(self):
        self.__run = False

        if self.__daemon is not None:
            self.__daemon.stop()

        if self.__stage is not None:
            self.__stage.close()

        if self.__client is not None:
            self.__client.close()

        if self.__logger_sock is not None:
            self.__logger_sock.close()


def main(stop_event):
    subsystem = SampleMotionSubsystem()
    print("Sample motion subsystem started.")

    try:
        while subsystem.ok() and not (stop_event is not None and stop_event.is_set()):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        subsystem.close()

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sample motion subsystem")
    _args = parser.parse_args()
    main(None)