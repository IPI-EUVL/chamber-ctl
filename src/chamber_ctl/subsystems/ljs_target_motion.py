from math import floor, isnan, pi
import multiprocessing
import os
import pickle
import struct
import time
import sys
import traceback
import mt_events
import serial

from threading import Lock
try:
    from labjack import ljm
except ImportError:
    print("LabJack LJM library not found, LabJack functionality will be unavailable.")
    ljm = None

from ipi_ecs.core import daemon
from ipi_ecs.core import tcp
from ipi_ecs.logging.client import LogClient

from chamber_ctl.subsystems.target_motion import TargetMotion, TargetMotionConfig

MM_TO_STEPS = 1000.0 * (50.0 / 13.5) * (50.0 / 23.0) * (50.0 / 52.0)  # Assuming 1000 steps per mm for LIN actuator

RADS_TO_STEPS = (3200) / (pi * 2)  # Assuming 2000 steps per revolution for rotary actuator

class LJSerialTargetMotion(TargetMotion):
    def __init__(self, config: TargetMotionConfig, logger: LogClient, port: str):
        self.__homing = False
        
        self.__logger = logger
        self.__config = config

        self.__do_jog_l = False

        self.__l_moving = False
        self.__should_home_l = False
        self.__l_homing = False

        self.__current_l = 0.0
        self.__current_r = 0.0


        self.__set_r = 0.0
        self.__r_offset = 0.0
        self.__rot_connection = None
        self.__rot_process = None
        self.__stop_event = None

        self.__l_speed = 0.0
        self.__r_speed = 0.0

        self.__target_l = float('nan')
        self.__target_r = float('nan')
        self.__last_l = float('nan')
        self.__last_r = float('nan')

        self.__jog_l = 0.0
        self.__jog_r = 0.0

        self.__lj_handle = None
        self.__port = port
        self.__l_serial = None

        self.__fault_reason = None
        self.__do_restart = False

        self.__lin_lock = Lock()

        self.__daemon = daemon.Daemon(exception_handler=self.handle_exception)
        self.__daemon.add(target=self.__l_thread)
        self.__daemon.add(target=self.__rot_thread)

    def start(self):
        self.__init_rot()
        self.__init_lin()

        self.__daemon.start()

    def set_raw_position(self, l_pos: float, r_pos: float):
        if l_pos != self.__current_l:
            raise ValueError("Cannot set raw position for L axis, it is controlled by the LIN actuator and cannot be directly set.")

        self.__r_offset = r_pos - (self.__current_r - self.__r_offset)

        return True

    def handle_exception(self, e: Exception):
        self.__log("Caught exception on daemon thread!", level="ERROR")
        for line in traceback.format_exception(None, e, e.__traceback__):
            for split in line.split('\n'):
                self.__log(split, level="ERROR")

    def __log(self, msg, level = "INFO", **data):
        if self.__logger is None:
            print(level, msg)
            return
        
        self.__logger.log(msg, level=level, l_type="SW", subsystem="Target Motion", **data)

    def close(self):
        if self.__rot_process is not None:
            print("Closing LabJack connection.")
            self.__stop_event.set()
            time.sleep(0.1)
            self.__rot_process.terminate()
            self.__rot_process.join(10.0)
            self.__rot_process = None

            self.__rot_connection.close()
            self.__rot_connection = None
        print("LabJack connection closed.")

        print("Stopping LIN thread.")
        self.__daemon.stop()
        time.sleep(0.5)
        print("LIN thread stopped.")

        if self.__l_serial is not None:
            print("Closing LIN serial connection.")
            self.__l_serial.close()
            self.__l_serial = None
        

    def __init_rot(self):
        parent_conn, child_conn = multiprocessing.Pipe()
        self.__rot_connection = parent_conn
        log_queue = multiprocessing.Queue()
        stop_event = multiprocessing.Event()

        self.__rot_process = multiprocessing.Process(target=_make_labjack_handler_subprocess, args=(child_conn, log_queue, stop_event, ))
        self.__rot_process.start()

        self.__stop_event = stop_event

    def __rot_thread(self, stop_flag: daemon.StopFlag):
        while stop_flag.run():
            time.sleep(0.02)

            if self.__rot_process is not None:
                self.__update_r()

    def __update_r(self):
        while self.__rot_connection.poll():
            pos, = self.__rot_connection.recv()
            self.__current_r = pos + self.__r_offset

        #print("Sending to LabJack subprocess:", (self.__target_r, self.__r_speed, self.__jog_r, self.__set_r))
        self.__rot_connection.send((self.__target_r - self.__r_offset, self.__r_speed, self.__jog_r, self.__set_r))

        if self.__set_r is not None:
            self.__set_r = None


    def __init_lin(self):
        try:
            self.__l_serial = serial.Serial(port=self.__port, baudrate=9600, bytesize=8, parity='N', stopbits=1, timeout=1)
            self.__stop_lin()

            assert self.__lin_command("CM11")
            assert self.__lin_command("SV20000")
            assert self.__lin_command("SF20000")
            assert self.__lin_command("SJ10000")
            assert self.__lin_command("SA50000")
            assert self.__lin_command("SD50000")
            assert self.__lin_command("SC00010")

        except AssertionError as e:
            self.__logger.log(f"Error initializing LIN communication: {e}", level="ERROR", l_type="SW", subsystem="Target Motion")
            self.__fault_reason = f"Error initializing LIN communication: {e}"
            time.sleep(0.1)
            raise e

    def __write_lin(self, line: str):
        if self.__l_serial is None:
            return

        with self.__lin_lock:        
            to_send = "01" + line + "\r"

            self.__l_serial.write(to_send.encode('ascii'))
            self.__l_serial.flush()

    def _read_lin(self):
        if self.__l_serial is None:
            return None
        
        start_t = time.monotonic()

        with self.__lin_lock:        
            while self.__l_serial.in_waiting == 0 and (time.monotonic() - start_t) < 1.0:
                time.sleep(0.1)

            response = self.__l_serial.read_until(b'\r')

            return response

    def __lin_transaction(self, line: str):
        if self.__l_serial is None:
            return None

        self.__write_lin(line)

        echoed = self._read_lin()
        response = self._read_lin()

        assert echoed is not None and echoed.decode('ascii').strip() == "01" + line, "LIN echo mismatch: expected: " + ("01" + line) + " got: " + str(echoed)

        response = response.decode('ascii').strip()
        return response
    
    def __lin_command(self, line: str):
        print(f"LIN command: {line}")
        ret = self.__lin_transaction(line)

        assert ret is not None and ret.startswith("01:"), "LIN command failed: received: " + str(ret)

        print(f"LIN command response: {ret}")

        self.__logger.log(f"LIN command: {line}, response: {ret}", level="DEBUG", l_type="REC", subsystem="Target Motion")

        if ret.startswith("01:OK"):
            return True
        else:
            return False

    def __get_lin_position(self):
        ret = self.__lin_transaction("OC")

        assert ret is not None and ret.startswith("01:")

        pos_str = ret[3:]
        try:
            position = int(pos_str)
            return position
        except ValueError:
            assert False, "LIN position parse error: received: " + str(ret)    

    def __home_lin(self):
        assert self.__lin_command("CM11")
        assert self.__lin_command("SV20000")
        assert self.__lin_command("SF20000")
        assert self.__lin_command("SJ10000")
        assert self.__lin_command("SA50000")
        assert self.__lin_command("SD50000")
        assert self.__lin_command("SC00010")
        assert self.__lin_command("HD"), "LIN home command failed."

    def __get_lin_current_op(self):
        ret = self.__lin_transaction("CO")

        assert ret is not None and ret.startswith("01:")

        op = ret[3:]

        print(f"LIN current operation: {op}")
        return op
    
    def __move_lin_to_position(self, position: int, speed: int):
        if self.__get_lin_current_op() == "Move":
            print("LIN already moving, stopping first.")
            self.__stop_lin()

        self.__logger.log(f"Moving LIN to position {position} at speed {speed}.", level="DEBUG", l_type="REC", subsystem="Target Motion")
        print(f"Moving LIN to position {position} at speed {speed}.")
        
        assert self.__lin_command(f"SV{speed}"), "LIN move command failed."
        assert self.__lin_command(f"MA{position}"), "LIN move command failed."

        self.__last_l = position

    def __jog_lin(self, speed: int):
        if self.__get_lin_current_op() == "Move":
            print("LIN already moving, stopping first.")
            self.__stop_lin()

        assert self.__lin_command(f"CV{speed}"), "LIN jog command failed."

    def __stop_lin(self):
        assert self.__lin_command(f"ST"), "LIN stop command failed."
            
    def __l_thread(self, stop_flag: daemon.StopFlag):
        while stop_flag.run():
            time.sleep(0.1)

            try:            
                if self.__l_serial is not None:
                    self.__update_l()
            except AssertionError as e:
                self.__logger.log(f"LIN error: {e}", level="ERROR", l_type="SW", subsystem="Target Motion")
                self.__do_restart = False
                self.__fault_reason = f"LIN error: {e}"

                while not self.__do_restart and stop_flag.run():
                    time.sleep(0.1)

                self.__l_serial.close()
                self.__l_serial = None
                time.sleep(1.0)
                self.__init_lin()

        print("LIN thread stopping, sending stop command.")
        self.__stop_lin()

    def get_status(self):
        if self.__fault_reason is not None:
            return False, self.__fault_reason

        return True, "OK"
    
    def reset(self):
        self.__fault_reason = None
        self.__do_restart = True

    def __update_l(self):
        self.__current_l = float(self.__get_lin_position()) / float(MM_TO_STEPS)

        c_op = self.__get_lin_current_op()
        self.__l_moving = c_op == "Move"
        self.__l_homing = c_op == "Home to datum"

        if self.__should_home_l:
            print("Homing LIN axis...")
            self.__l_homing = True
            self.__home_lin()
            self.__should_home_l = False

        if self.__l_homing:
            return
        
        #print(f"LIN Current Position: {self.__current_l} mm")
        #print(f"LIN Target Position: {self.__target_l} mm")
        #print(f"LIN Last Position: {self.__last_l / float(MM_TO_STEPS)} mm")

        #print(f"LIN Speed: {self.__l_speed} mm/s")
        #print(f"LIN Moving: {self.__l_moving}")

        """self.__logger.log(f"LIN Current Position: {self.__current_l} mm", level="DEBUG", l_type="REC", subsystem="Target Motion")
        self.__logger.log(f"LIN Target Position: {self.__target_l} mm", level="DEBUG", l_type="REC", subsystem="Target Motion")
        self.__logger.log(f"LIN Last Position: {self.__last_l / float(MM_TO_STEPS)} mm", level="DEBUG", l_type="REC", subsystem="Target Motion")
        self.__logger.log(f"LIN Speed: {self.__l_speed} mm/s", level="DEBUG", l_type="REC", subsystem="Target Motion")
        self.__logger.log(f"LIN Moving: {self.__l_moving}", level="DEBUG", l_type="REC", subsystem="Target Motion")
        self.__logger.log(f"LIN Jog: {self.__jog_l} mm/s", level="DEBUG", l_type="REC", subsystem="Target Motion")
        self.__logger.log(f"LIN Jog NaN Check: {isnan(self.__jog_l) or abs(self.__jog_l) < 1e-3}", level="DEBUG", l_type="REC", subsystem="Target Motion")
        self.__logger.log(f"LIN Jog NaN Check: {(abs((self.__current_l / float(MM_TO_STEPS)) - self.__target_l) > 1e-3 or isnan(self.__last_l))}", level="DEBUG", l_type="REC", subsystem="Target Motion")
        self.__logger.log(f"LIN Jog NaN Check: {self.__l_speed > 0.0}", level="DEBUG", l_type="REC", subsystem="Target Motion")
        """
        if (isnan(self.__jog_l) or abs(self.__jog_l) < 1e-3) and (not self.__l_moving) and (abs((self.__current_l / float(MM_TO_STEPS)) - self.__target_l) > 1e-3 or isnan(self.__last_l)) and abs(self.__l_speed) > 0.0:
            self.__move_lin_to_position(int(self.__target_l * MM_TO_STEPS), int(self.__l_speed * MM_TO_STEPS))
            print(f"Reissuing LIN move command issued to position {self.__target_l} mm at speed {self.__l_speed} mm/s.")
            self.__logger.log(f"Reissuing LIN move command issued to position {self.__target_l} mm at speed {self.__l_speed} mm/s.", level="DEBUG", l_type="REC", subsystem="Target Motion")

        if self.__l_speed < 0.01 and self.__l_moving:
            self.__stop_lin()            
        elif not isnan(self.__target_l):
            if (abs((self.__last_l / float(MM_TO_STEPS)) - self.__target_l) > 1e-3 or isnan(self.__last_l)) and self.__l_speed > 0.0:
                self.__move_lin_to_position(int(self.__target_l * MM_TO_STEPS), int(self.__l_speed * MM_TO_STEPS))
        elif not isnan(self.__jog_l):
            if self.__jog_l != 0.0:
                self.__jog_lin(int(self.__jog_l * MM_TO_STEPS))
            elif self.__do_jog_l:
                self.__stop_lin()
                self.__do_jog_l = False


    def move_to_position(self, l_pos: float, r_pos: float, speed_l: float, speed_r: float):
        self.__target_l = l_pos
        self.__target_r = r_pos
        self.__l_speed = speed_l
        self.__r_speed = speed_r

        #print(f"Moving to position L: {l_pos}, R: {r_pos} at speeds L: {speed_l}, R: {speed_r}")

        self.__do_update_l = True
        self.__do_update_r = True
        
    def get_position(self):
        return self.__current_l, self.__current_r
    
    def get_target_position(self):
        return self.__target_l, self.__target_r
    
    def is_moving(self):
        #print(f"Checking is_moving: Current L: {self.__current_l}, Target L: {self.__target_l}, Current R: {self.__current_r}, Target R: {self.__target_r}, L moving: {self.__l_moving}")
        if isnan(self.__target_l):
            return False
        if isnan(self.__target_r):
            return False

        return not ((self.__current_r - self.__target_r) < 2e-2) or self.__l_moving
    
    def jog(self, delta_l: float, delta_r: float):
        self.__jog_l = delta_l
        self.__jog_r = delta_r

        self.__l_speed = abs(self.__jog_l)
        self.__r_speed = abs(self.__jog_r)

        self.__target_r = self.__current_r

        self.__do_jog_l = True

    def is_jogging(self):
        return not (self.__jog_l == 0.0 and self.__jog_r == 0.0)
    
    def ready_for_move(self):
        return not self.is_homing()
    
    def home(self):
        self.__should_home_l = True
        self.__set_r = 0.0

    def is_homing(self):
        return self.__l_homing or self.__should_home_l
        #return self.__get_lin_current_op() == "Home to datum"

    def stop(self):
        self.__r_speed = 0.0
        self.__l_speed = 0.0
        pass

class LabJackHandlerSubprocess:
    PUL_PLUS = "FIO4"
    PUL_MIN = "FIO5"
    DIR_PLUS = "FIO6"
    DIR_MIN = "FIO7"
    def __init__(self, conn, log_queue, stop_event):
        print("LabJack Handler subprocess started.")
        self.__conn = conn
        self.__log_queue = log_queue
        self.__stop_event = stop_event
        self.__init_lj()

        #print("LabJack Handler subprocess started.")
        self.__target_r = float('nan')
        self.__r_speed = 0.0
        self.__jog_r = 0.0
        self.__current_r = 0.0

        #print("LabJack Handler subprocess started.")
        self.__daemon = daemon.Daemon()
        self.__daemon.add(target=self.__conn_thread)
        self.__daemon.add(target=self.__rot_thread)
        self.__daemon.add(target=self.__send_thread)
        self.__daemon.start()

    def __init_lj(self):
        print("Initializing LabJack...")
        self.__lj_handle = ljm.openS("ANY", "ANY", "ANY")
        print("Initializing LabJack...")
        ljm.eWriteName(self.__lj_handle, "DIO_INHIBIT", 0)
        ljm.eWriteName(self.__lj_handle, "DIO_ANALOG_ENABLE", 0)
        ljm.eWriteName(self.__lj_handle, self.PUL_PLUS, 0)
        ljm.eWriteName(self.__lj_handle, self.PUL_MIN, 0)
        ljm.eWriteName(self.__lj_handle, self.DIR_PLUS, 0)
        ljm.eWriteName(self.__lj_handle, self.DIR_MIN, 0)
        print("LabJack initialized.")

    def ok(self):
        return self.__daemon.is_ok()

    def __conn_thread(self, stop_flag: daemon.StopFlag):
        #print("LabJack Handler connection thread started.")
        while stop_flag.run() and not self.__stop_event.is_set():
            time.sleep(0.02)

            while self.__conn.poll():
                target_r, r_speed, jog_r, set_pos = self.__conn.recv()
                self.__target_r = target_r
                self.__r_speed = max(min(r_speed, 1.0), -1.0)
                self.__jog_r = jog_r

            #print(f"LabJack Handler received target_r: {target_r}, r_speed: {r_speed}, jog_r: {jog_r}, set_pos: {set_pos}")

            if set_pos:
                self.__current_r = set_pos

    def __send_thread(self, stop_flag: daemon.StopFlag):
        while stop_flag.run() and not self.__stop_event.is_set():
            time.sleep(0.02)
            #print(f"LabJack Handler sending current_r: {self.__current_r}")
            self.__conn.send((self.__current_r, ))

    def __direction_set(self, direction):
        if direction:
            ljm.eWriteName(self.__lj_handle, self.DIR_PLUS, 1)
            ljm.eWriteName(self.__lj_handle, self.DIR_MIN, 0)
        else:  
            ljm.eWriteName(self.__lj_handle, self.DIR_PLUS, 0)
            ljm.eWriteName(self.__lj_handle, self.DIR_MIN, 0)

    def __pulse(self):
        ljm.eWriteName(self.__lj_handle, self.PUL_PLUS, 1)
        time.sleep(0.000001)  
        ljm.eWriteName(self.__lj_handle, self.PUL_PLUS, 0)
        time.sleep(0.000001)

    def __rot_thread(self, stop_flag: daemon.StopFlag):
        l_t = time.monotonic()
        target_r = 0
        current_r = 0.0

        has_reset = False

        while stop_flag.run() and not self.__stop_event.is_set():
            t = time.monotonic()
            dt = t - l_t
            l_t = t

            #if not isnan(self.__target_r):
            #    mt = floor(self.__target_r / 6.281)
            #    mc = current_r % (6.281 * RADS_TO_STEPS)
            #    current_r = int(mt * RADS_TO_STEPS * 6.281) + mc

            if self.__jog_r != 0.0:
                self.__target_r += self.__jog_r * dt

            if self.__r_speed < 0.01 and self.__jog_r == 0.0:
                self.__target_r = self.__current_r

            if target_r < self.__target_r:
                target_r += self.__r_speed * dt
                if target_r > self.__target_r:
                    target_r = self.__target_r
            elif target_r > self.__target_r:
                target_r -= self.__r_speed * dt
                if target_r < self.__target_r:
                    target_r = self.__target_r
            else:
                time.sleep(0.01)
                continue

            """if not has_reset and abs(target_r) > 0.01:
                print("Resetting LabJack position to target:", target_r)
                has_reset = True
                target_r = self.__target_r
                current_r = target_r
                self.__current_r = current_r"""
            

            #print(f"LabJack Handler - Target R: {self.__target_r}, Current R: {self.__current_r}, Target R (internal): {target_r}, R Speed: {self.__r_speed}, Jog R: {self.__jog_r}")
            
            if current_r != target_r * RADS_TO_STEPS:
                steps = floor(abs(current_r - target_r * RADS_TO_STEPS))

                direction = (target_r * RADS_TO_STEPS) > current_r
                self.__direction_set(direction)

                #steps = steps % int(RADS_TO_STEPS * 0.1)

                for _ in range(steps):
                    self.__pulse()
                    current_r += 1.0 if direction else -1.0

                self.__current_r = current_r / RADS_TO_STEPS
                if abs(self.__current_r - self.__target_r) < 5e-3:
                    self.__current_r = self.__target_r

def _make_labjack_handler_subprocess(p, log_queue, stop_event):
    print("Starting LabJack handler subprocess...")
    _ph = LabJackHandlerSubprocess(p, log_queue, stop_event)

    while _ph.ok():
        time.sleep(1)
                
def main():
    config = TargetMotionConfig(max_l_size=300.0)
    config.traverse_speed_l = 2.0
    config.traverse_speed_r = 2.0
    logger_sock = tcp.TCPClientSocket()
    logger_sock.connect(("127.0.0.1", 11751))
    logger_sock.start()

    logger = LogClient(logger_sock)
    target_motion = LJSerialTargetMotion(config, logger, port="COM3")

    try:
        print("Starting test...")
        target_motion.start()

        time.sleep(0.1)

        print("Starting test...")

        target_motion.move_to_position(00.0, 6.28, 1.5, 2)
        time.sleep(10.0)
        target_motion.move_to_position(0.0, 0.0, 1.5, 2)
        
        while True:
            current_l, current_r = target_motion.get_position()
            print(f"Current Position - L: {current_l}, R: {current_r}")
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        print("Closing target motion...")
        target_motion.close()
        logger_sock.close()

if __name__ == "__main__":
    main()