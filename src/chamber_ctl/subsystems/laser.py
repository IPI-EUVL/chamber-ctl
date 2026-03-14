import time
import uuid
import pickle
import os
import struct
import segment_bytes

from ipi_ecs.core import daemon
from ipi_ecs.dds.magics import OP_OK
import ipi_ecs.dds.client as client
import ipi_ecs.dds.magics as magics
import ipi_ecs.dds.types as types
import ipi_ecs.core.tcp as tcp
import ipi_ecs.db.db_library as db_library

from ipi_ecs.logging.client import LogClient
from ipi_ecs.subsystems.experiment_client import ExperimentClient

from chamber_ctl.subsystems import uuids
from chamber_ctl.subsystems.exposure_controller import ExposureSettings

class DummyLaserSyncProvider:
    def __init__(self, laser_warmup_time=5, chopper_startup_time=5):
        self.__laser_warmup_time = laser_warmup_time
        self.__chopper_startup_time = chopper_startup_time

        self.__target_phase = 0
        self.__laser_on = False
        self.__laser_started_time = None

        self.__chopper_on = False
        self.__chopper_started_time = None

        self.__skew_rate = 5 # degrees per second
        self.__current_phase = 0

        self.__daemon = daemon.Daemon()
        self.__daemon.add(target=self.__thread)

    def set_target_phase(self, phase):
        self.__target_phase = phase

    def get_target_phase(self):
        return self.__target_phase
    
    def get_current_phase(self):
        return self.__current_phase
    
    def set_chopper_on(self, on):
        self.__chopper_on = on
        self.__chopper_started_time = time.monotonic() if on else None

    def get_chopper_on(self):
        return self.__chopper_on
    
    def get_chopper_starting_up(self):
        if not self.__chopper_on:
            return False
        if self.__chopper_started_time is None:
            return False
        
        return (time.monotonic() - self.__chopper_started_time) < self.__chopper_startup_time

    def set_laser_on(self, on):
        self.__laser_on = on
        self.__laser_started_time = time.monotonic() if on else None

    def get_laser_on(self):
        return self.__laser_on
    
    def get_laser_warming_up(self):
        if not self.__laser_on:
            return False
        if self.__laser_started_time is None:
            return False
        
        return (time.monotonic() - self.__laser_started_time) < self.__laser_warmup_time
    
    def __thread(self, stop_flag: daemon.StopFlag):
        last_time = time.monotonic()
        while stop_flag.run():
            time.sleep(0.01)
            
            t = time.monotonic()
            dt = t - last_time
            last_time = t

            if self.get_chopper_starting_up():
                continue

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


class LaserSyncStatus:
    def __init__(self, laser_on: bool, laser_warming_up: bool, chopper_on: bool, chopper_starting_up: bool, current_phase: float, target_phase: float):
        self.laser_on = laser_on
        self.laser_warming_up = laser_warming_up
        self.chopper_on = chopper_on
        self.chopper_starting_up = chopper_starting_up
        self.current_phase = current_phase
        self.target_phase = target_phase

    def encode(self) -> bytes:
        return pickle.dumps(self)

    @staticmethod
    def decode(data: bytes) -> "LaserSyncStatus":
        return pickle.loads(data)


class LaserSyncSubsystem(ExperimentClient):
    PHASE_EPSILON = 1e-2

    def __init__(self):
        self.__run = True
        c_uuid = uuid.uuid4()

        self.__logger_sock = tcp.TCPClientSocket()
        self.__logger_sock.connect(("127.0.0.1", 11751))
        self.__logger_sock.start()

        self.__logger = LogClient(self.__logger_sock, origin_uuid=c_uuid)

        self.__did_config = False
        self.__subsystem = None

        self.__preinit_handle = None
        self.__start_handle = None
        self.__stop_handle = None

        self.__test_preinit_handle = None
        self.__test_init_handle = None
        self.__test_stop_handle = None

        self.__experiment_active = False
        self.__test_active = False

        self.__preinit_phase = 0.0
        self.__target_phase = 0.0
        self.__status_publisher = None

        self.__SAVE_PATH = os.path.join(os.environ["EUVL_PATH"], "datasets")
        self.__library = db_library.Library(self.__SAVE_PATH)
        self.__load_config()

        self.__sync = DummyLaserSyncProvider()
        self.__sync.start()

        def _on_ready():
            if self.__did_config:
                return

            self.__did_config = True
            sh = self.__client.register_subsystem("Laser Sync Controller", uuids.UUID_LASER_CONTROLLER)
            self.__on_got_subsystem(sh)

        self.__client = client.DDSClient(c_uuid, logger=self.__logger)
        self.__client.when_ready().then(_on_ready)

        self.__daemon = daemon.Daemon()
        self.__daemon.add(target=self.__thread)
        self.__daemon.start()

        super().__init__("exposure", "Laser Sync Controller", self.__logger)
        self.register_experiment_settings_type(ExposureSettings)

    def __load_config(self):
        recs = self.__library.query({"name": "Laser Sync Controller Save State", "limit": 1})

        if not recs:
            rec = self.__library.create_entry("Laser Sync Controller Save State", "Saves the phase configuration of the Laser Sync Controller")
        else:
            rec = recs[0]

        try:
            res = rec.resource("laser_config.bin", "Laser Config", "rb")
            b_data = res.read()
            res.close()

            b_preinit_phase, b_target_phase = segment_bytes.decode(b_data)
            self.__preinit_phase = float(pickle.loads(b_preinit_phase))
            self.__target_phase = float(pickle.loads(b_target_phase))

            self.__logger.log(
                f"Loaded laser config preinit_phase={self.__preinit_phase}, target_phase={self.__target_phase}",
                level="DEBUG",
                l_type="CTRL",
                subsystem="Laser Sync Controller",
                event="load_laser_config",
            )
        except (FileNotFoundError, ValueError, IndexError, TypeError, struct.error, pickle.UnpicklingError):
            self.__logger.log(
                "No saved laser config found, or saved config is invalid. Using defaults.",
                level="WARN",
                l_type="CTRL",
                subsystem="Laser Sync Controller",
                event="load_laser_config",
            )

            res = rec.resource("laser_config.bin", "Laser Config", "wb")
            bdata = segment_bytes.encode([pickle.dumps(self.__preinit_phase), pickle.dumps(self.__target_phase)])
            res.write(bdata)
            res.close()

    def __save_config(self):
        recs = self.__library.query({"name": "Laser Sync Controller Save State", "limit": 1})

        if not recs:
            self.__logger.log(
                "Could not find laser config record while saving. Creating new record.",
                level="WARN",
                l_type="CTRL",
                subsystem="Laser Sync Controller",
                event="save_laser_config",
            )
            rec = self.__library.create_entry("Laser Sync Controller Save State", "Saves the phase configuration of the Laser Sync Controller")
        else:
            rec = recs[0]

        self.__logger.log(
            f"Saving laser config preinit_phase={self.__preinit_phase}, target_phase={self.__target_phase}",
            level="DEBUG",
            l_type="CTRL",
            subsystem="Laser Sync Controller",
            event="save_laser_config",
        )

        res = rec.resource("laser_config.bin", "Laser Config", "wb")
        bdata = segment_bytes.encode([pickle.dumps(self.__preinit_phase), pickle.dumps(self.__target_phase)])
        res.write(bdata)
        res.close()

    def __phase_at(self, phase: float) -> bool:
        return abs(self.__sync.get_current_phase() - phase) <= self.PHASE_EPSILON

    def __preinit_ready(self) -> bool:
        return (
            self.__sync.get_laser_on()
            and self.__sync.get_chopper_on()
            and not self.__sync.get_laser_warming_up()
            and not self.__sync.get_chopper_starting_up()
            and self.__phase_at(self.__preinit_phase)
        )

    def __init_ready(self) -> bool:
        return self.__phase_at(self.__target_phase)

    def __thread(self, stop_flag: daemon.StopFlag):
        last_status_publish = time.monotonic()
        while stop_flag.run() and self.__run:
            time.sleep(0.01)

            if self.__preinit_handle is not None and self.__preinit_ready():
                self._on_did_preinit(b": preinit complete.")
                self.__preinit_handle = None

            if self.__start_handle is not None and self.__init_ready():
                self._on_did_start(b": target phase reached.")
                self.__start_handle = None

            if self.__stop_handle is not None:
                if not self.__sync.get_laser_on() and not self.__sync.get_chopper_on():
                    self._on_did_stop(b": laser and chopper disabled.")
                    self.__stop_handle = None
                    self.__experiment_active = False

            if self.__test_preinit_handle is not None and self.__preinit_ready():
                self.__test_preinit_handle.ret(OP_OK + b": test preinit complete.")
                self.__test_preinit_handle = None

            if self.__test_init_handle is not None and self.__init_ready():
                self.__test_init_handle.ret(OP_OK + b": test init complete.")
                self.__test_init_handle = None

            if self.__test_stop_handle is not None:
                if not self.__sync.get_laser_on() and not self.__sync.get_chopper_on():
                    self.__test_stop_handle.ret(OP_OK + b": test stop complete.")
                    self.__test_stop_handle = None
                    self.__test_active = False

            if self.__status_publisher is not None and (time.monotonic() - last_status_publish) > 0.1:
                last_status_publish = time.monotonic()
                self.__status_publisher.value = self.__get_status().encode()

    def __get_status(self) -> LaserSyncStatus:
        return LaserSyncStatus(
            laser_on=self.__sync.get_laser_on(),
            laser_warming_up=self.__sync.get_laser_warming_up(),
            chopper_on=self.__sync.get_chopper_on(),
            chopper_starting_up=self.__sync.get_chopper_starting_up(),
            current_phase=self.__sync.get_current_phase(),
            target_phase=self.__sync.get_target_phase(),
        )

    def _on_preinit(self, handle) -> bytes:
        self.__experiment_active = True

        self.__sync.set_laser_on(True)
        self.__sync.set_chopper_on(True)
        self.__sync.set_target_phase(self.__preinit_phase)

        self.__preinit_handle = handle

        self.__logger.log(
            f"Laser preinit started (phase={self.__preinit_phase}).",
            level="INFO",
            l_type="CTRL",
            subsystem="Laser Sync Controller",
            event="preinit_laser",
        )

        return b": laser/chopper enabled, waiting for preinit phase."

    def _on_start(self, handle) -> bytes:
        self.__sync.set_target_phase(self.__target_phase)
        self.__start_handle = handle

        self.__logger.log(
            f"Laser init started (target phase={self.__target_phase}).",
            level="INFO",
            l_type="CTRL",
            subsystem="Laser Sync Controller",
            event="init_laser",
        )

        return b": phase transition started."

    def _on_stop(self, handle) -> bytes:
        self.__sync.set_laser_on(False)
        self.__sync.set_chopper_on(False)
        self.__stop_handle = handle

        self.__logger.log(
            "Laser stop requested.",
            level="INFO",
            l_type="CTRL",
            subsystem="Laser Sync Controller",
            event="stop_laser",
        )

        return b": disabling laser/chopper."

    def _on_continue_state(self):
        if self.__experiment_active or self.__preinit_handle is not None or self.__start_handle is not None or self.__stop_handle is not None:
            return True, self.EXP_IN_PROGRESS

        return False, b"Laser/chopper are not active."

    def _can_preinit(self, settings, state):
        if self.__test_active or self.__test_preinit_handle is not None or self.__test_init_handle is not None or self.__test_stop_handle is not None:
            return False, b"Laser test control is active."

        return super()._can_preinit(settings, state)

    def __on_test_preinit(self, _s_uuid, _param, handle: client._EventHandler._IncomingEventHandle):
        if self.__experiment_active or self.__preinit_handle is not None or self.__start_handle is not None or self.__stop_handle is not None:
            handle.fail(b"Cannot start laser test preinit while experiment control is active.")
            return

        if self.__test_preinit_handle is not None or self.__test_init_handle is not None or self.__test_stop_handle is not None:
            handle.fail(b"Laser test sequence already in progress.")
            return

        self.__test_active = True
        self.__sync.set_laser_on(True)
        self.__sync.set_chopper_on(True)
        self.__sync.set_target_phase(self.__preinit_phase)

        self.__test_preinit_handle = handle
        handle.feedback(magics.OP_IN_PROGRESS + b": test preinit started.")

    def __on_test_init(self, _s_uuid, _param, handle: client._EventHandler._IncomingEventHandle):
        if self.__experiment_active or self.__preinit_handle is not None or self.__start_handle is not None or self.__stop_handle is not None:
            handle.fail(b"Cannot start laser test init while experiment control is active.")
            return

        if self.__test_preinit_handle is not None or self.__test_init_handle is not None or self.__test_stop_handle is not None:
            handle.fail(b"Laser test sequence already in progress.")
            return

        self.__test_active = True
        self.__sync.set_target_phase(self.__target_phase)

        self.__test_init_handle = handle
        handle.feedback(magics.OP_IN_PROGRESS + b": test init started.")

    def __on_test_stop(self, _s_uuid, _param, handle: client._EventHandler._IncomingEventHandle):
        if self.__experiment_active or self.__preinit_handle is not None or self.__start_handle is not None or self.__stop_handle is not None:
            handle.fail(b"Cannot stop laser test while experiment control is active.")
            return

        if self.__test_preinit_handle is not None or self.__test_init_handle is not None or self.__test_stop_handle is not None:
            handle.fail(b"Laser test sequence already in progress.")
            return

        self.__sync.set_laser_on(False)
        self.__sync.set_chopper_on(False)

        self.__test_stop_handle = handle
        handle.feedback(magics.OP_IN_PROGRESS + b": test stop started.")

    def __on_preinit_phase_write(self, _h, _requester, value: float):
        self.__preinit_phase = value
        self.__logger.log(
            f"Updated preinit phase to {value}.",
            level="DEBUG",
            l_type="CTRL",
            subsystem="Laser Sync Controller",
            event="set_preinit_phase",
        )
        return (magics.TRANSOP_STATE_OK, OP_OK)

    def __on_preinit_phase_read(self, _requester):
        return (magics.TRANSOP_STATE_OK, self.__preinit_phase)

    def __on_target_phase_write(self, _h, _requester, value: float):
        self.__target_phase = value
        self.__logger.log(
            f"Updated target phase to {value}.",
            level="DEBUG",
            l_type="CTRL",
            subsystem="Laser Sync Controller",
            event="set_target_phase",
        )
        return (magics.TRANSOP_STATE_OK, OP_OK)

    def __on_target_phase_read(self, _requester):
        return (magics.TRANSOP_STATE_OK, self.__target_phase)

    def __on_got_subsystem(self, handle: client._RegisteredSubsystemHandle):
        self.__subsystem = handle

        handle.add_event_handler(b"laser_test_preinit").on_called(self.__on_test_preinit)
        handle.add_event_handler(b"laser_test_init").on_called(self.__on_test_init)
        handle.add_event_handler(b"laser_test_stop").on_called(self.__on_test_stop)

        self.__status_publisher = handle.get_kv_property(b"status", False, True, True)
        self.__status_publisher.set_type(types.ByteTypeSpecifier())
        self.__status_publisher.value = self.__get_status().encode()

        preinit_phase_kv = handle.add_kv_handler(b"preinit_phase")
        preinit_phase_kv.set_type(types.FloatTypeSpecifier())
        preinit_phase_kv.on_set(self.__on_preinit_phase_write)
        preinit_phase_kv.on_get(self.__on_preinit_phase_read)

        target_phase_kv = handle.add_kv_handler(b"target_phase")
        target_phase_kv.set_type(types.FloatTypeSpecifier())
        target_phase_kv.on_set(self.__on_target_phase_write)
        target_phase_kv.on_get(self.__on_target_phase_read)

        self._setup_subsystem(handle)

    def ok(self):
        return self.__run and self.__client.ok() and self.__daemon.is_ok()

    def close(self):
        self.__daemon.stop()
        self.__sync.stop()

        self.__save_config()
        self.__library.close()

        self.__client.close()
        self.__logger_sock.close()

        self.__run = False


def main(stop_event):
    subsystem = LaserSyncSubsystem()
    print("Laser sync subsystem started.")

    try:
        while subsystem.ok() and not (stop_event is not None and stop_event.is_set()):
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        print("Shutting down laser sync subsystem...")
        subsystem.close()


if __name__ == "__main__":
    main(None)