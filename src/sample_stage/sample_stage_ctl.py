import RPi.GPIO as GPIO
import math
import time
import threading
import socket
import board
import adafruit_pcf8574
from digitalio import Direction, Pull
import time

SERVER_INCOMING_PORT_NUM = 11755

ACTIVE_LOW = False
ACTIVE_HIGH = True

class Stepper:
    def __init__(self, step, dir, maxvel):
        self.__p_step = step
        self.__p_dir = dir
        self.__maxvel = maxvel

        self.__homing_active = False
        self.__home_position = None
        self.__homing_speed = maxvel

        self.__can_move = False

        GPIO.setup(self.__p_dir, GPIO.OUT) #lin step
        GPIO.setup(self.__p_step, GPIO.OUT) #lin dir

        self.target = 0
        self.position = 0

        self.__stop_flag = False

        threading.Thread(target=self.__thread, daemon=True).start()

    def __thread(self):
        while not self.__stop_flag:
            if self.__can_move and self.position != self.target:
                vel = self.__maxvel

                if self.__homing_active:
                    vel = self.__homing_speed

                if self.position > self.target:
                    vel = -vel

                if vel > 0:
                    self.position += 1
                    GPIO.output(self.__p_dir, GPIO.HIGH)
                else:
                    self.position -= 1
                    GPIO.output(self.__p_dir, GPIO.LOW)
                
                GPIO.output(self.__p_step, GPIO.HIGH)
                time.sleep(abs(1.0 / vel) / 2.0)
                GPIO.output(self.__p_step, GPIO.LOW)
                time.sleep(abs(1.0 / vel) / 2.0)

            else:
                time.sleep(0.01)

    def close(self):
        self.__stop_flag = True

    def set_target(self, target):
        self.target = target

    def get_position(self):
        return self.position
    
    def is_moving(self):
        return self.position != self.target
    
    def can_move(self, val):
        self.__can_move = val

    def set_position(self, position):
        self.position = position

    def set_homing_state(self, to_home, speed):
        self.__homing_active = to_home
        self.__homing_speed = speed
        self.__home_position = None

    def get_home_position(self):
        return self.__home_position
    
    def trig_home(self):
        if not self.__homing_active:
            return

        self.__home_position = self.position
        self.target = 0
        self.position = 0
        self.__homing_active = False

class LimitSwitchController:
    def __init__(self, addr, int_pin):
        i2c = board.I2C()
        self.__pcf = adafruit_pcf8574.PCF8574(i2c, address=addr)

        self.__int = int_pin
        GPIO.setup(self.__int, GPIO.IN, pull_up_down=GPIO.PUD_UP)

        self.__steppers = []

        self.__shutdown_flag = False
        threading.Thread(target=self.__thread, daemon=True).start()

    def __thread(self):
        while not self.__shutdown_flag:
            while True:
                time.sleep(0.001)
                if GPIO.input(self.__int) == GPIO.LOW:
                    break

            print("State change detected!")

            for stepper, pin in self.__steppers:
                while True:
                    try:
                        value = pin.value
                        print(value)
                        if value:
                            stepper.trig_home()
                    except OSError:
                        #print("Fuck")
                        time.sleep(0.001)
                        continue
                    
                    break

    def attach(self, stepper : Stepper, pin_n):
        pin = self.__pcf.get_pin(pin_n)
        pin.switch_to_input(pull=Pull.UP)

        self.__steppers.append((stepper, pin))

class StepperController:
    def __init__(self, en):
        self.__p_en = en
        self.__steppers = []
        self.__enabled = False

        self.__last_move = 0

        GPIO.setup(self.__p_en, GPIO.OUT)

        self.__stop_flag = False
        threading.Thread(target=self.__thread, daemon=True).start()

    def __thread(self):
        while not self.__stop_flag:
            time.sleep(0.1)

            to_move = False
            for stepper in self.__steppers:
                to_move = to_move or stepper.is_moving()

            if to_move:
                self.__last_move = time.time()

            if to_move and not self.__enabled:
                GPIO.output(self.__p_en, GPIO.LOW)
                time.sleep(2)

                for stepper in self.__steppers:
                    stepper.can_move(True)

                self.__enabled = True

            if not to_move and self.__enabled and (time.time() - self.__last_move) > 10.0:
                for stepper in self.__steppers:
                    stepper.can_move(False)

                time.sleep(2)
                GPIO.output(self.__p_en, GPIO.HIGH)

                self.__enabled = False

    def close(self):
        self.__stop_flag = True

    def set_stepper(self, stepper, target):
        self.__steppers[stepper].set_target(target)

    def set_homing_state(self, stepper, state, speed):
        self.__steppers[stepper].set_homing_state(state, speed)

    def get_stepper(self, stepper):
        return self.__steppers[stepper].get_position()
    
    def get_home_position(self, stepper):
        return self.__steppers[stepper].get_home_position()

    def is_moving(self, stepper):
        return self.__steppers[stepper].is_moving()

    def is_enabled(self):
        return self.__enabled
    
    def set_stepper_position(self, stepper, target):
        self.__steppers[stepper].set_position(target)

    def add_stepper(self, stepper):
        self.__steppers.append(stepper)

    def num_steppers(self):
        return len(self.__steppers)

class StepperNetworkComm:
    def __init__(self, ctl):
        self.__ctl = ctl
        self.__sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.__remote_addr = None
        self.__c_seq = -1

        self.__stop_flag = False

        self.__server = threading.Thread(target=self.__server_thread, daemon=True) 
        self.__server.start()

        self.__receiver = threading.Thread(target=self.__receive_thread, daemon=True) 
        self.__receiver.start()

    def close(self):
        print("Shutting down server...")
        self.__stop_flag = True
        time.sleep(1)
        self.__sock.close()


    def __server_thread(self):
        print("Starting server thread!")

        while not self.__stop_flag:
            self.__send_status()
            time.sleep(0.05)

    def __send_status(self):
        data = ""

        data += f"S,{self.__c_seq};"
        data += f"E,{self.__ctl.is_enabled()};"

        for stepper in range(self.__ctl.num_steppers()):
            data += f"P,{stepper},{self.__ctl.get_stepper(stepper)};"
            data += f"M,{stepper},{self.__ctl.is_moving(stepper)};"
            data += f"H,{stepper},{self.__ctl.get_home_position(stepper)};"

        if self.__remote_addr is not None:
            self.__sock.sendto(data.encode("utf-8"), self.__remote_addr)

    def __receive_thread(self):
        print(f"Binding port {SERVER_INCOMING_PORT_NUM} for incoming connections")
        self.__sock.bind(("0.0.0.0", SERVER_INCOMING_PORT_NUM))

        while not self.__stop_flag:
            self.__receive()

    def __receive(self):
        try:
            data, addr = self.__sock.recvfrom(1024)
            print(f"Received {data} from {addr}")

            data_str = data.decode("utf-8").strip().split(',')
            if data_str[0] == "REQ_CONN":
                print(f"Received connection request from {addr}")
                self.__remote_addr = addr
                return
            
            seq = data_str[0]
            command = data_str[1]

            self.__c_seq = seq

            if command == "MOVE":
                self.__ctl.set_stepper(int(data_str[2]), int(data_str[3]))
            elif command == "SET":
                self.__ctl.set_stepper_position(int(data_str[2]), int(data_str[3]))
            elif command == "HOME":
                self.__ctl.set_homing_state(int(data_str[2]), data_str[3] == "T", int(data_str[4]))

        except ConnectionResetError:
            pass
        except IndexError:
            raise
            print("Received invalid command")
        except ValueError:
            raise
            print("Error while parsing message")


ROT_STEP = 20
ROT_DIR = 21
LIN_STEP = 16
LIN_DIR = 26
ENABLE = 5

GPIO.setmode(GPIO.BCM)

rot = Stepper(20, 21, 400)
lin = Stepper(16, 26, 20000)

ctl = StepperController(ENABLE)
ctl.add_stepper(rot)
ctl.add_stepper(lin)

lim = LimitSwitchController(0x27, 7)
lim.attach(rot, 1)

#ctl.set_homing_state(0, True, 100)
#ctl.set_stepper(0, 1600)

udp = StepperNetworkComm(ctl)


try:
    while True:
#        print(f"Rot: {ctl.get_stepper(0)}, lin: {ctl.get_stepper(1)}")
        time.sleep(1)

finally:
    udp.close()
    ctl.close()
    GPIO.cleanup()
