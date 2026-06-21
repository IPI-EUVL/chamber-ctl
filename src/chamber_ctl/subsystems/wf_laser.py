import time
import pyvisa
import serial

from ipi_ecs.core import daemon

from chamber_ctl.subsystems.laser_provider import LaserSyncProvider


PORT = "COM4"
class WFLaserSyncProvider(LaserSyncProvider):
    def __init__(self, laser_warmup_time=5, chopper_startup_time=5):
        self.__laser_warmup_time = laser_warmup_time
        self.__chopper_startup_time = chopper_startup_time

        self.__target_phase = 0
        self.__laser_on = False
        self.__laser_started_time = None

        self.__chopper_on = False
        self.__chopper_started_time = None

        self.__skew_rate = 1
        self.__current_phase = 0
        self.__initial_phase = 0

        self.__last_chopper_read_time = 0

        self.__port = serial.Serial(PORT, 115200, 8, "N", 1)
        self.waveform = pyvisa.ResourceManager().open_resource('USB0::0x0957::0x1507::MY48009073::INSTR')
    
        self.__daemon = daemon.Daemon()
        self.__daemon.add(target=self.__thread)

    def __send_chopper_cmd(self, cmd):
        if self.__port is None:
            return False
        
        try:
            #print(f"{cmd}\r".encode("utf-8"))
            self.__port.write(f"{cmd}\r".encode("utf-8"))
            echo = self.__port.read_until(expected=b"\r").decode("utf-8").strip()
            return True
        except serial.SerialException:
            self.__port = None
            return False
        
    def __send_and_read_chopper_cmd(self, cmd):
        if self.__port is None:
            return None
        
        try:
            self.__port.write(f"{cmd}\r".encode("utf-8"))
            time.sleep(0.1)
            echo = self.__port.read_until(expected=b"\r").decode("utf-8").strip()
            response = self.__port.read_until(expected=b"\r").decode("utf-8").strip()

            #print(f"Chopper cmd echo: '{echo}', response: '{response}'")
            return response
        except serial.SerialException:
            self.__port = None
            return None

    def __set_chopper(self, state):
        print(f"Setting chopper {'ON' if state else 'OFF'}...")

        if self.__send_chopper_cmd(f"enable={1 if state else 0}"):
            self.__chopper_on = state
            return True
        
        self.__chopper_on = False
        return False
    
    def __set_wf_phase(self, phase):
        self.waveform.write(f"BURSt:PHASe {phase}")

    def set_target_phase(self, phase) -> tuple[bool, str]:
        self.__target_phase = phase
        return True, f"Target phase set to {phase:.3f}."

    def set_skew_rate(self, skew_rate: float) -> tuple[bool, str]:
        if skew_rate < 0:
            return False, "Skew rate must be non-negative."
        self.__skew_rate = float(skew_rate)
        return True, f"Skew rate set to {self.__skew_rate:.3f} deg/s."

    def get_skew_rate(self) -> float:
        return self.__skew_rate

    def set_laser_warmup_time(self, warmup_time: float) -> tuple[bool, str]:
        if warmup_time < 0:
            return False, "Laser warmup time must be non-negative."
        self.__laser_warmup_time = float(warmup_time)
        return True, f"Laser warmup time set to {self.__laser_warmup_time:.3f} s."

    def get_laser_warmup_time(self) -> float:
        return self.__laser_warmup_time

    def set_chopper_startup_time(self, startup_time: float) -> tuple[bool, str]:
        if startup_time < 0:
            return False, "Chopper startup time must be non-negative."
        self.__chopper_startup_time = float(startup_time)
        return True, f"Chopper startup time set to {self.__chopper_startup_time:.3f} s."

    def get_chopper_startup_time(self) -> float:
        return self.__chopper_startup_time

    def set_current_phase(self, phase) -> tuple[bool, str]:
        self.__current_phase = phase
        return True, f"Current phase set to {phase:.3f}."

    def set_initial_phase(self, phase) -> tuple[bool, str]:
        self.__initial_phase = phase
        return True, f"Initial phase set to {phase:.3f}."

    def get_initial_phase(self):
        return self.__initial_phase

    def get_target_phase(self):
        return self.__target_phase

    def get_current_phase(self):
        return self.__current_phase
    
    def get_current_chopper_frequency(self):
        print("Querying chopper frequency...")
        response = self.__send_and_read_chopper_cmd("refoutfreq?")
        if response is None:
            return None
        
        print(f"Received chopper frequency response: '{response}'")
        
        try:
            freq = float(response)
            return freq
        except ValueError:
            print(f"Unexpected chopper frequency response: '{response}'")
            return None
        
    def __run_chopper(self):
        for _ in range(5):
            self.__set_chopper(True)
            for __ in range(5):
                time.sleep(1)

                if not self.read_chopper_on():
                    print("Chopper stopped, probably due to overspeed, retrying...")
                    continue

            if self.read_chopper_on():
                return True, 'Chopper enabled and running at {:.2f} Hz.'.format(self.get_current_chopper_frequency())
        
        self.__set_chopper(False)
        return False, 'Failed to start chopper after multiple attempts.'

    def set_chopper_on(self, on) -> tuple[bool, str]:
        self.__chopper_on = on

        if on:
            ok, r = self.__run_chopper()
            self.__chopper_started_time = time.monotonic()
            return ok, r
        else:
            self.__set_chopper(False)
            return True, "Chopper disabled."
    
    def read_chopper_on(self):
        fx = self.get_current_chopper_frequency()

        if fx is not None and fx > 10:
            self.__chopper_on = True
        else:
            self.__chopper_on = False

        return self.__chopper_on

    def get_chopper_on(self):
        if time.monotonic() - self.__last_chopper_read_time > 1:
            self.__last_chopper_read_time = time.monotonic()
            self.read_chopper_on()

        return self.__chopper_on

    def get_chopper_starting_up(self):
        if not self.__chopper_on:
            return False
        if self.__chopper_started_time is None:
            return False

        return (time.monotonic() - self.__chopper_started_time) < self.__chopper_startup_time

    def set_laser_on(self, on) -> tuple[bool, str]:
        self.waveform.write("OUTPut " + ("ON" if on else "OFF"))
        self.__laser_on = on
        self.__laser_started_time = time.monotonic() if on else None
        if on:
            return True, "Laser enabled, warmup in progress."
        return True, "Laser disabled."

    def get_laser_on(self):
        return self.__laser_on

    def get_laser_warming_up(self):
        if not self.__laser_on:
            return False
        if self.__laser_started_time is None:
            return False

        return (time.monotonic() - self.__laser_started_time) < self.__laser_warmup_time
    
    def do_single_shot(self, shut_phase: float, open_phase: float, expose_time: float):
        if self.get_chopper_starting_up():
            return False, "Cannot do single shot while chopper is starting up."
        if self.get_laser_warming_up():
            return False, "Cannot do single shot while laser is warming up."
        
        self.set_target_phase(open_phase) # Open chopper
        while abs(self.__current_phase - open_phase) > 1e-2:
            time.sleep(0.01)

        time.sleep(expose_time)

        self.set_target_phase(shut_phase) # Close chopper
        while abs(self.__current_phase - shut_phase) > 1e-2:
            time.sleep(0.01)
            
        return True, f"Single shot completed with expose time {expose_time:.3f} s."

    def __thread(self, stop_flag: daemon.StopFlag):
        last_time = time.monotonic()
        while stop_flag.run():
            time.sleep(0.1)

            t = time.monotonic()
            dt = t - last_time
            last_time = t

            if self.get_chopper_starting_up():
                continue

            if not self.get_laser_on():
                continue

            self.__set_wf_phase(self.__current_phase)

            if self.__current_phase != self.__target_phase:
                if self.__current_phase < self.__target_phase:
                    self.__current_phase += self.__skew_rate * dt
                    if self.__current_phase > self.__target_phase:
                        self.__current_phase = self.__target_phase
                else:
                    self.__current_phase -= self.__skew_rate * dt
                    if self.__current_phase < self.__target_phase:
                        self.__current_phase = self.__target_phase

    def start(self):
        self.__daemon.start()

    def stop(self):
        self.__daemon.stop()
