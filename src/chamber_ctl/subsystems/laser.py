import time
import traceback
import uuid
import pickle
import os
import struct
import queue
import segment_bytes

from ipi_ecs.core import daemon
from ipi_ecs.dds.magics import OP_OK
import ipi_ecs.dds.client as client
import ipi_ecs.dds.subsystem as dds_subsystem
import ipi_ecs.dds.magics as magics
from ipi_ecs.dds.subsystem import StatusItem
import ipi_ecs.dds.types as types
import ipi_ecs.core.tcp as tcp
import ipi_ecs.db.db_library as db_library

from ipi_ecs.logging.client import LogClient
from ipi_ecs.subsystems.experiment_client import ExperimentClient

from chamber_ctl.subsystems import uuids
from chamber_ctl.subsystems.exposure_controller import ExposureSettings
from chamber_ctl.subsystems.laser_provider import LaserSyncProvider, LaserSyncProviderStatus
from chamber_ctl.subsystems.wf_laser import WFLaserSyncProvider
from euv_acquisition.health import AcquisitionHealth
from euv_acquisition.timing import LaserTimingState

class DummyLaserSyncProvider:
    def __init__(self, laser_warmup_time=5, chopper_startup_time=5):
        self.__laser_warmup_time = laser_warmup_time
        self.__chopper_startup_time = chopper_startup_time

        self.__target_phase = 0
        self.__laser_on = False
        self.__laser_started_time = None

        self.__chopper_on = False
        self.__chopper_started_time = None
        self.__chopper_frequency_hz = 192.0

        self.__skew_rate = 5 # degrees per second
        self.__current_phase = 0
        self.__initial_phase = 0

        self.__daemon = daemon.Daemon()
        self.__daemon.add(target=self.__thread)

    def set_target_phase(self, phase):
        self.__target_phase = phase
        return True, f"Target phase set to {phase:.3f}."

    def refresh_hardware_status(self) -> LaserSyncProviderStatus:
        return self.get_hardware_status()

    def get_hardware_status(self) -> LaserSyncProviderStatus:
        chopper_on = self.get_chopper_on()
        return LaserSyncProviderStatus(
            desired_laser_on=self.__laser_on,
            laser_on=self.__laser_on,
            desired_chopper_on=self.__chopper_on,
            chopper_on=chopper_on,
            chopper_starting_up=self.get_chopper_starting_up(),
            chopper_spinning=chopper_on,
            target_chopper_frequency_hz=int(self.__chopper_frequency_hz),
            measured_chopper_frequency_hz=self.__chopper_frequency_hz if chopper_on else 0.0,
            chopper_connected=True,
            waveform_connected=True,
        )

    def set_skew_rate(self, skew_rate: float):
        if skew_rate < 0:
            return False, "Skew rate must be non-negative."
        self.__skew_rate = float(skew_rate)
        return True, f"Skew rate set to {self.__skew_rate:.3f} deg/s."

    def get_skew_rate(self):
        return self.__skew_rate

    def set_laser_warmup_time(self, warmup_time: float):
        if warmup_time < 0:
            return False, "Laser warmup time must be non-negative."
        self.__laser_warmup_time = float(warmup_time)
        return True, f"Laser warmup time set to {self.__laser_warmup_time:.3f} s."

    def get_laser_warmup_time(self):
        return self.__laser_warmup_time

    def set_chopper_startup_time(self, startup_time: float):
        if startup_time < 0:
            return False, "Chopper startup time must be non-negative."
        self.__chopper_startup_time = float(startup_time)
        return True, f"Chopper startup time set to {self.__chopper_startup_time:.3f} s."

    def get_chopper_startup_time(self):
        return self.__chopper_startup_time

    def set_current_phase(self, phase):
        self.__current_phase = phase
        return True, f"Current phase set to {phase:.3f}."

    def set_initial_phase(self, phase):
        self.__initial_phase = phase
        return True, f"Initial phase set to {phase:.3f}."

    def get_initial_phase(self):
        return self.__initial_phase

    def get_target_phase(self):
        return self.__target_phase
    
    def get_current_phase(self):
        return self.__current_phase
    
    def set_chopper_on(self, on):
        self.__chopper_on = on
        self.__chopper_started_time = time.monotonic() if on else None
        if on:
            return True, "Chopper enabled."
        return True, "Chopper disabled."

    def get_chopper_on(self):
        return self.__chopper_on
    
    def get_chopper_starting_up(self):
        if not self.__chopper_on:
            return False
        if self.__chopper_started_time is None:
            return False
        
        return (time.monotonic() - self.__chopper_started_time) < self.__chopper_startup_time

    def set_chopper_frequency_hz(self, frequency_hz: float) -> tuple[bool, str]:
        if frequency_hz <= 0:
            return False, "Chopper frequency must be positive."
        self.__chopper_frequency_hz = float(frequency_hz)
        return True, f"Chopper frequency set to {self.__chopper_frequency_hz:.3f} Hz."

    def get_chopper_frequency_hz(self) -> float:
        return self.__chopper_frequency_hz

    def set_laser_on(self, on):
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
        # In a real implementation, this would trigger the laser and chopper for a single exposure of the given time.
        # Here we just simulate the timing.
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
    def __init__(self, laser_on: bool, laser_warming_up: bool, chopper_on: bool, chopper_starting_up: bool, current_phase: float, target_phase: float, preinit_phase=None, configured_target_phase=None, chopper_frequency_hz=None, desired_laser_on=None, desired_chopper_on=None, chopper_spinning=None, target_chopper_frequency_hz=None, chopper_connected=None, waveform_connected=None, chopper_recovery_exhausted=False, chopper_error=None, waveform_error=None):
        self.laser_on = laser_on
        self.laser_warming_up = laser_warming_up
        self.chopper_on = chopper_on
        self.chopper_starting_up = chopper_starting_up
        self.current_phase = current_phase
        self.target_phase = target_phase
        self.preinit_phase = preinit_phase
        self.configured_target_phase = configured_target_phase
        self.chopper_frequency_hz = chopper_frequency_hz
        self.desired_laser_on = desired_laser_on
        self.desired_chopper_on = desired_chopper_on
        self.chopper_spinning = chopper_spinning
        self.target_chopper_frequency_hz = target_chopper_frequency_hz
        self.chopper_connected = chopper_connected
        self.waveform_connected = waveform_connected
        self.chopper_recovery_exhausted = chopper_recovery_exhausted
        self.chopper_error = chopper_error
        self.waveform_error = waveform_error

    def encode(self) -> bytes:
        return pickle.dumps(self)

    @staticmethod
    def decode(data: bytes) -> "LaserSyncStatus":
        status = pickle.loads(data)
        for name, default in (
            ("desired_laser_on", None),
            ("desired_chopper_on", None),
            ("chopper_spinning", None),
            ("target_chopper_frequency_hz", None),
            ("chopper_connected", None),
            ("waveform_connected", None),
            ("chopper_recovery_exhausted", False),
            ("chopper_error", None),
            ("waveform_error", None),
        ):
            if not hasattr(status, name):
                setattr(status, name, default)
        return status


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

        self.__test_laser_on_handle = None
        self.__test_laser_off_handle = None
        self.__test_chopper_on_handle = None
        self.__test_chopper_off_handle = None
        self.__test_set_phase_handle = None

        self.__do_timed_exposure_handle = None
        self.__do_continuous_exposure_handle = None
        self.__laser_shut_handle = None

        self.__experiment_active = False
        self.__test_active = False

        self.__preinit_phase = 0.0
        self.__target_phase = 0.0
        self.__initial_phase = 0.0
        self.__skew_rate = 1.0
        self.__laser_warmup_time = 5.0
        self.__chopper_startup_time = 5.0
        self.__requested_chopper_frequency_hz = None
        self.__status_publisher = None
        self.__timing_status_publisher = None
        self.__status_item_cache = dict()
        self.__acquisition_health = None
        self.__acquisition_health_received_at = 0.0
        self.__target_status = None
        self.__interlock_closed = False
        self.__interlock_close_pending = False
        self.__interlock_reopen_pending = False

        self.__setter_queue = queue.Queue()

        self.__preinit_setup_pending = False
        self.__start_setup_pending = False
        self.__stop_setup_pending = False

        self.__test_preinit_setup_pending = False
        self.__test_init_setup_pending = False
        self.__test_stop_setup_pending = False

        self.__config_save_queue = queue.Queue()
        self.__config_save_in_progress = False

        self.__SAVE_PATH = os.path.join(os.environ["EUVL_PATH"], "datasets")
        self.__load_config()

        self.__sync: LaserSyncProvider = WFLaserSyncProvider()
        print(self.__sync)
        ok, status = self.__sync.set_initial_phase(self.__initial_phase)
        if not ok:
            raise RuntimeError(f"Failed to set initial phase during startup: {status}")
        ok, status = self.__sync.set_skew_rate(self.__skew_rate)
        if not ok:
            raise RuntimeError(f"Failed to set skew rate during startup: {status}")
        ok, status = self.__sync.set_laser_warmup_time(self.__laser_warmup_time)
        if not ok:
            raise RuntimeError(f"Failed to set laser warmup time during startup: {status}")
        ok, status = self.__sync.set_chopper_startup_time(self.__chopper_startup_time)
        if not ok:
            raise RuntimeError(f"Failed to set chopper startup time during startup: {status}")
        ok, status = self.__sync.set_target_phase(self.__initial_phase)
        if not ok:
            raise RuntimeError(f"Failed to set target phase during startup: {status}")
        ok, status = self.__sync.set_current_phase(self.__initial_phase)
        if not ok:
            raise RuntimeError(f"Failed to set current phase during startup: {status}")
        self.__sync.start()

        def _on_ready():
            if self.__did_config:
                return

            self.__did_config = True
            sh = self.__client.register_subsystem("Laser Sync", uuids.UUID_LASER_CONTROLLER)
            self.__on_got_subsystem(sh)

        self.__client = client.DDSClient(c_uuid, logger=self.__logger)
        self.__client.when_ready().then(_on_ready)

        self.__daemon = daemon.Daemon(exception_handler=self.handle_exception)
        self.__daemon.add(target=self.__thread)
        self.__daemon.add(target=self.__setter_worker_thread)
        self.__daemon.add(target=self.__config_saver_thread)
        self.__daemon.start()

        super().__init__("exposure", "Laser Sync", self.__logger)
        self.register_experiment_settings_type(ExposureSettings)

    def handle_exception(self, e: Exception):
        self.__log("Caught exception on daemon thread!", level="ERROR")
        for line in traceback.format_exception(None, e, e.__traceback__):
            for split in line.split('\n'):
                self.__log(split, level="ERROR")

    def __log(self, msg, level = "INFO", **data):
        if self.__logger is None:
            print(level, msg)
            return
        
        self.__logger.log(msg, level=level, l_type="SW", subsystem="Laser Sync Controller", **data)

    def __load_config(self):
        library = db_library.Library(self.__SAVE_PATH)
        recs = library.query({"name": "Laser Sync Controller Save State", "limit": 1})

        try:
            if not recs:
                rec = library.create_entry("Laser Sync Controller Save State", "Saves the phase configuration of the Laser Sync Controller")
            else:
                rec = recs[0]

            res = rec.resource("laser_config.bin", "Laser Config", "rb")
            b_data = res.read()
            res.close()

            values = segment_bytes.decode(b_data)
            self.__preinit_phase = float(pickle.loads(values[0]))
            self.__target_phase = float(pickle.loads(values[1]))
            if len(values) >= 3:
                self.__initial_phase = float(pickle.loads(values[2]))
            else:
                self.__initial_phase = 0.0
            if len(values) >= 4:
                self.__skew_rate = float(pickle.loads(values[3]))
            else:
                self.__skew_rate = 1.0
            if len(values) >= 5:
                self.__laser_warmup_time = float(pickle.loads(values[4]))
            else:
                self.__laser_warmup_time = 5.0
            if len(values) >= 6:
                self.__chopper_startup_time = float(pickle.loads(values[5]))
            else:
                self.__chopper_startup_time = 5.0

            self.__logger.log(
                f"Loaded laser config preinit_phase={self.__preinit_phase}, target_phase={self.__target_phase}, initial_phase={self.__initial_phase}, skew_rate={self.__skew_rate}, laser_warmup_time={self.__laser_warmup_time}, chopper_startup_time={self.__chopper_startup_time}",
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

            if not recs:
                rec = library.create_entry("Laser Sync Controller Save State", "Saves the phase configuration of the Laser Sync Controller")
            else:
                rec = recs[0]

            res = rec.resource("laser_config.bin", "Laser Config", "wb")
            bdata = segment_bytes.encode([
                pickle.dumps(self.__preinit_phase),
                pickle.dumps(self.__target_phase),
                pickle.dumps(self.__initial_phase),
                pickle.dumps(self.__skew_rate),
                pickle.dumps(self.__laser_warmup_time),
                pickle.dumps(self.__chopper_startup_time),
            ])
            res.write(bdata)
            res.close()
        finally:
            library.close()

    def __save_config(self):
        library = db_library.Library(self.__SAVE_PATH)
        recs = library.query({"name": "Laser Sync Controller Save State", "limit": 1})

        try:
            if not recs:
                self.__logger.log(
                    "Could not find laser config record while saving. Creating new record.",
                    level="WARN",
                    l_type="CTRL",
                    subsystem="Laser Sync Controller",
                    event="save_laser_config",
                )
                rec = library.create_entry("Laser Sync Controller Save State", "Saves the phase configuration of the Laser Sync Controller")
            else:
                rec = recs[0]

            self.__logger.log(
                f"Saving laser config preinit_phase={self.__preinit_phase}, target_phase={self.__target_phase}, initial_phase={self.__initial_phase}, skew_rate={self.__skew_rate}, laser_warmup_time={self.__laser_warmup_time}, chopper_startup_time={self.__chopper_startup_time}",
                level="DEBUG",
                l_type="CTRL",
                subsystem="Laser Sync Controller",
                event="save_laser_config",
            )

            res = rec.resource("laser_config.bin", "Laser Config", "wb")
            bdata = segment_bytes.encode([
                pickle.dumps(self.__preinit_phase),
                pickle.dumps(self.__target_phase),
                pickle.dumps(self.__initial_phase),
                pickle.dumps(self.__skew_rate),
                pickle.dumps(self.__laser_warmup_time),
                pickle.dumps(self.__chopper_startup_time),
            ])
            res.write(bdata)
            res.close()
        finally:
            library.close()

    def __request_config_save(self):
        self.__config_save_queue.put(1)

    def __config_saver_thread(self, stop_flag: daemon.StopFlag):
        while stop_flag.run() and self.__run:
            try:
                self.__config_save_queue.get(timeout=0.2)
            except queue.Empty:
                continue

            while not self.__config_save_queue.empty():
                try:
                    self.__config_save_queue.get_nowait()
                except queue.Empty:
                    break

            self.__config_save_in_progress = True
            try:
                self.__save_config()
            except Exception as exc:
                self.__logger.log(
                    f"Failed to save laser config: {exc}",
                    level="ERROR",
                    l_type="CTRL",
                    subsystem="Laser Sync Controller",
                    event="save_laser_config",
                )
            finally:
                self.__config_save_in_progress = False

    @staticmethod
    def __to_bytes(status: str | bytes | None) -> bytes:
        if status is None:
            return b""
        if isinstance(status, bytes):
            return status
        return str(status).encode("utf-8", errors="replace")

    def __provider_set(self, handle, step_label: str, setter, *args):
        if handle is not None:
            handle.feedback(magics.OP_IN_PROGRESS + f": {step_label}...".encode("utf-8"))

        try:
            ok, status = setter(*args)
        except Exception as exc:
            self.__log(
                f"Provider operation {step_label} raised an exception: {exc}",
                level="ERROR",
                event="laser_provider_operation",
                step=step_label,
            )
            return False, self.__to_bytes(f"{step_label} failed: {exc}")
        b_status = self.__to_bytes(status)

        if handle is not None and b_status:
            handle.feedback(magics.OP_IN_PROGRESS + b": " + b_status)

        if not ok:
            self.__log(
                f"Provider operation {step_label} failed: {b_status.decode('utf-8', errors='replace')}",
                level="ERROR",
                event="laser_provider_operation",
                step=step_label,
            )

        return bool(ok), b_status

    def __put_status_item_if_changed(self, code: int, severity: int, message: str):
        if self.__subsystem is None:
            return

        status = (severity, message)
        if self.__status_item_cache.get(code) == status:
            return

        self.__subsystem.put_status_item(StatusItem(severity, code, message))
        self.__status_item_cache[code] = status
        if severity == StatusItem.STATE_ALARM:
            self.__log(message, level="ERROR", event="laser_status_alarm", status_code=code)
        elif severity == StatusItem.STATE_WARN:
            self.__log(message, level="WARNING", event="laser_status_warning", status_code=code)

    def __clear_status_item_if_exists(self, code: int):
        if self.__subsystem is None:
            return

        if self.__subsystem.get_status_item_exists(code):
            self.__subsystem.clear_status_item(code)

        self.__status_item_cache.pop(code, None)

    def __record_chopper_on_result(self, ok: bool, status: bytes | str | None):
        if ok:
            self.__clear_status_item_if_exists(200)
            return

        msg = self.__to_bytes(status).decode("utf-8", errors="replace")
        self.__put_status_item_if_changed(200, StatusItem.STATE_ALARM, f"Chopper failed to turn on: {msg}")

    def __get_provider_status(self) -> LaserSyncProviderStatus | None:
        refresh = getattr(self.__sync, "refresh_hardware_status", None)
        if not callable(refresh):
            return None
        try:
            return refresh()
        except Exception as exc:
            self.__log(
                f"Unable to refresh physical laser status: {exc}",
                level="ERROR",
                event="laser_provider_status",
            )
            return None

    def __update_status_items(self):
        provider_status = self.__get_provider_status()
        laser_on = provider_status.laser_on if provider_status is not None else self.__sync.get_laser_on()
        chopper_on = provider_status.chopper_on if provider_status is not None else self.__sync.get_chopper_on()
        laser_warming = self.__sync.get_laser_warming_up()
        chopper_starting = provider_status.chopper_starting_up if provider_status is not None else self.__sync.get_chopper_starting_up()

        if laser_warming:
            self.__put_status_item_if_changed(0, StatusItem.STATE_INFO, "Warming up")
        elif chopper_starting:
            self.__put_status_item_if_changed(0, StatusItem.STATE_INFO, "Starting")
        elif laser_on and chopper_on and not self.__phase_at(self.__sync.get_target_phase()):
            self.__put_status_item_if_changed(0, StatusItem.STATE_INFO, "Moving phase")
        elif laser_on and chopper_on:
            self.__put_status_item_if_changed(0, StatusItem.STATE_INFO, "Ready")
        elif provider_status is not None and provider_status.desired_chopper_on and provider_status.chopper_spinning:
            self.__put_status_item_if_changed(0, StatusItem.STATE_INFO, "Chopper outside target tolerance")
        elif provider_status is not None and provider_status.desired_chopper_on:
            self.__put_status_item_if_changed(0, StatusItem.STATE_INFO, "Chopper recovering")
        elif laser_on:
            self.__put_status_item_if_changed(0, StatusItem.STATE_INFO, "Laser on")
        elif chopper_on:
            self.__put_status_item_if_changed(0, StatusItem.STATE_INFO, "Chopper on")
        else:
            self.__put_status_item_if_changed(0, StatusItem.STATE_INFO, "Idle")

        if laser_on and not chopper_on:
            self.__put_status_item_if_changed(100, StatusItem.STATE_ALARM, "Laser enabled while chopper is not at target frequency.")
            self.__put_status_item_if_changed(101, StatusItem.STATE_ALARM, "Laser has no valid chopper sync source.")
        else:
            self.__clear_status_item_if_exists(100)
            self.__clear_status_item_if_exists(101)

        if provider_status is not None and not provider_status.chopper_connected:
            detail = provider_status.chopper_error or "No connection to chopper controller."
            self.__put_status_item_if_changed(102, StatusItem.STATE_ALARM, f"Chopper controller disconnected: {detail}")
        else:
            self.__clear_status_item_if_exists(102)

        if provider_status is not None and not provider_status.waveform_connected:
            detail = provider_status.waveform_error or "No connection to waveform generator."
            self.__put_status_item_if_changed(103, StatusItem.STATE_ALARM, f"Waveform generator disconnected: {detail}")
        else:
            self.__clear_status_item_if_exists(103)

        if provider_status is not None and provider_status.chopper_recovery_exhausted:
            detail = provider_status.chopper_error or "Automatic recovery limit reached."
            self.__put_status_item_if_changed(104, StatusItem.STATE_ALARM, f"Chopper recovery exhausted: {detail}")
        else:
            self.__clear_status_item_if_exists(104)

        if chopper_on:
            self.__put_status_item_if_changed(1, StatusItem.STATE_INFO, "Chopper on")
        else:
            self.__clear_status_item_if_exists(1)

        if laser_on:
            self.__put_status_item_if_changed(2, StatusItem.STATE_INFO, "Laser on")
        else:
            self.__clear_status_item_if_exists(2)

        if laser_warming and chopper_starting:
            self.__put_status_item_if_changed(3, StatusItem.STATE_INFO, "Laser warming, chopper starting")
        elif laser_warming:
            self.__put_status_item_if_changed(3, StatusItem.STATE_INFO, "Laser warming")
        elif chopper_starting:
            self.__put_status_item_if_changed(3, StatusItem.STATE_INFO, "Chopper starting")
        else:
            self.__clear_status_item_if_exists(3)

    def __reset_to_initial_phase(self, handle=None):
        ok, status = self.__provider_set(handle, "Setting initial target phase", self.__sync.set_target_phase, self.__initial_phase)
        if not ok:
            return False, status

        ok, status = self.__provider_set(handle, "Resetting current phase", self.__sync.set_current_phase, self.__initial_phase)
        if not ok:
            return False, status

        return True, f"Reset to initial phase {self.__initial_phase:.3f}.".encode("utf-8")

    def __enqueue_setter_job(self, job_name: str, payload=None):
        self.__setter_queue.put((job_name, payload))

    def __setter_worker_thread(self, stop_flag: daemon.StopFlag):
        while stop_flag.run() and self.__run:
            try:
                job_name, payload = self.__setter_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            try:
                if job_name == "exp_preinit":
                    self.__execute_exp_preinit_setters()
                elif job_name == "exp_preinit_laser":
                    self.__execute_exp_preinit_laser_setters()
                elif job_name == "exp_start":
                    self.__execute_exp_start_setters()
                elif job_name == "exp_stop":
                    self.__execute_exp_stop_setters()
                elif job_name == "test_preinit":
                    self.__execute_test_preinit_setters()
                elif job_name == "test_preinit_laser":
                    self.__execute_test_preinit_laser_setters()
                elif job_name == "test_init":
                    self.__execute_test_init_setters()
                elif job_name == "test_stop":
                    self.__execute_test_stop_setters()
                elif job_name == "test_laser_on":
                    self.__execute_test_laser_on_setters()
                elif job_name == "test_laser_off":
                    self.__execute_test_laser_off_setters()
                elif job_name == "test_chopper_on":
                    self.__execute_test_chopper_on_setters()
                elif job_name == "test_chopper_off":
                    self.__execute_test_chopper_off_setters()
                elif job_name == "test_set_phase":
                    self.__execute_test_set_phase_setters(float(payload))
                elif job_name == "do_timed_exposure":
                    self.__execute_do_timed_exposure(float(payload))
                elif job_name == "do_continuous_exposure":
                    self.__execute_do_continuous_exposure()
                elif job_name == "laser_shut":
                    self.__execute_laser_shut()
                elif job_name == "interlock_close":
                    self.__execute_interlock_close()
                elif job_name == "interlock_reopen":
                    self.__execute_interlock_reopen()
            except Exception as exc:
                self.__logger.log(
                    f"Setter worker error in {job_name}: {exc}",
                    level="ERROR",
                    l_type="CTRL",
                    subsystem="Laser Sync Controller",
                    event="setter_worker",
                )

    def __execute_exp_preinit_setters(self):
        handle = self.__preinit_handle
        if handle is None:
            self.__preinit_setup_pending = False
            return

        ok, status = self.__provider_set(
            handle,
            "Setting chopper frequency",
            self.__sync.set_chopper_frequency_hz,
            self.__requested_chopper_frequency_hz,
        )
        if not ok:
            handle.fail(b"Preinit failed: " + status)
            self.__preinit_handle = None
            self.__preinit_setup_pending = False
            self.__experiment_active = False
            return

        ok, status = self.__provider_set(handle, "Turning chopper on", self.__sync.set_chopper_on, True)
        self.__record_chopper_on_result(ok, status)
        if not ok:
            handle.fail(b"Preinit failed: " + status)
            self.__preinit_handle = None
            self.__preinit_setup_pending = False
            self.__experiment_active = False
            return

        self.__preinit_setup_pending = False
        handle.feedback(b"Chopper enable requested, waiting for target frequency before enabling laser.")

    def __execute_exp_preinit_laser_setters(self):
        handle = self.__preinit_handle
        if handle is None:
            self.__preinit_setup_pending = False
            return

        ok, status = self.__provider_set(handle, "Turning laser on", self.__sync.set_laser_on, True)
        if not ok:
            handle.fail(b"Preinit failed while enabling laser: " + status)
            self.__preinit_handle = None
            self.__preinit_setup_pending = False
            self.__experiment_active = False
            return

        ok, status = self.__provider_set(handle, "Setting preinit phase", self.__sync.set_target_phase, self.__preinit_phase)
        if not ok:
            handle.fail(b"Preinit failed: " + status)
            self.__preinit_handle = None
            self.__preinit_setup_pending = False
            self.__experiment_active = False
            return

        self.__preinit_setup_pending = False
        handle.feedback(b"Laser enabled, waiting for warmup and preinit phase.")

    def __execute_exp_start_setters(self):
        handle = self.__start_handle
        if handle is None:
            self.__start_setup_pending = False
            return

        ok, status = self.__provider_set(handle, "Setting target phase", self.__sync.set_target_phase, self.__target_phase)
        if not ok:
            handle.fail(b"Init failed: " + status)
            self.__start_handle = None
            self.__start_setup_pending = False
            return

        self.__start_setup_pending = False
        handle.feedback(b"Init setup complete, waiting for target phase.")

    def __execute_exp_stop_setters(self):
        handle = self.__stop_handle
        if handle is None:
            self.__stop_setup_pending = False
            return

        ok, status = self.__provider_set(handle, "Turning laser off", self.__sync.set_laser_on, False)
        if not ok:
            handle.fail(b"Stop failed: " + status)
            self.__stop_handle = None
            self.__stop_setup_pending = False
            return

        ok, status = self.__reset_to_initial_phase(handle)
        if not ok:
            handle.fail(b"Stop failed: " + status)
            self.__stop_handle = None
            self.__stop_setup_pending = False
            return

        self.__stop_setup_pending = False
        handle.feedback(b"Stop complete, waiting for laser to disable.")

    def __execute_test_preinit_setters(self):
        handle = self.__test_preinit_handle
        if handle is None:
            self.__test_preinit_setup_pending = False
            return

        ok, status = self.__provider_set(handle, "Turning chopper on", self.__sync.set_chopper_on, True)
        self.__record_chopper_on_result(ok, status)
        if not ok:
            self.__test_active = False
            handle.fail(status)
            self.__test_preinit_handle = None
            self.__test_preinit_setup_pending = False
            return

        self.__test_preinit_setup_pending = False
        handle.feedback(magics.OP_IN_PROGRESS + b": chopper enable requested; waiting for target frequency.")

    def __execute_test_preinit_laser_setters(self):
        handle = self.__test_preinit_handle
        if handle is None:
            self.__test_preinit_setup_pending = False
            return

        ok, status = self.__provider_set(handle, "Turning laser on", self.__sync.set_laser_on, True)
        if not ok:
            self.__test_active = False
            handle.fail(status)
            self.__test_preinit_handle = None
            self.__test_preinit_setup_pending = False
            return

        ok, status = self.__provider_set(handle, "Setting preinit phase", self.__sync.set_target_phase, self.__preinit_phase)
        if not ok:
            self.__test_active = False
            handle.fail(status)
            self.__test_preinit_handle = None
            self.__test_preinit_setup_pending = False
            return

        self.__test_preinit_setup_pending = False
        handle.feedback(magics.OP_IN_PROGRESS + b": laser enabled; waiting for warmup and preinit phase.")

    def __execute_test_init_setters(self):
        handle = self.__test_init_handle
        if handle is None:
            self.__test_init_setup_pending = False
            return

        ok, status = self.__provider_set(handle, "Setting target phase", self.__sync.set_target_phase, self.__target_phase)
        if not ok:
            self.__test_active = False
            handle.fail(status)
            self.__test_init_handle = None
            self.__test_init_setup_pending = False
            return

        self.__test_init_setup_pending = False
        handle.feedback(magics.OP_IN_PROGRESS + b": test init setup complete.")

    def __execute_test_stop_setters(self):
        handle = self.__test_stop_handle
        if handle is None:
            self.__test_stop_setup_pending = False
            return

        ok, status = self.__provider_set(handle, "Turning laser off", self.__sync.set_laser_on, False)
        if not ok:
            handle.fail(status)
            self.__test_stop_handle = None
            self.__test_stop_setup_pending = False
            return

        ok, status = self.__reset_to_initial_phase(handle)
        if not ok:
            handle.fail(status)
            self.__test_stop_handle = None
            self.__test_stop_setup_pending = False
            return

        self.__test_stop_setup_pending = False
        handle.feedback(magics.OP_IN_PROGRESS + b": test stop setup complete.")

    def __execute_test_laser_on_setters(self):
        handle = self.__test_laser_on_handle
        if handle is None:
            return

        ok, status = self.__provider_set(handle, "Turning laser on", self.__sync.set_laser_on, True)
        self.__test_laser_on_handle = None
        if not ok:
            handle.fail(status)
            return

        self.__refresh_test_active()
        handle.ret(OP_OK + b": " + status)

    def __execute_test_laser_off_setters(self):
        handle = self.__test_laser_off_handle
        if handle is None:
            return

        ok, status = self.__provider_set(handle, "Turning laser off", self.__sync.set_laser_on, False)
        if not ok:
            self.__test_laser_off_handle = None
            handle.fail(status)
            return

        ok, status = self.__reset_to_initial_phase(handle)
        self.__test_laser_off_handle = None
        if not ok:
            handle.fail(status)
            return

        self.__refresh_test_active()
        handle.ret(OP_OK + b": " + status)

    def __execute_test_chopper_on_setters(self):
        print("Executing test chopper on setters")
        handle = self.__test_chopper_on_handle
        if handle is None:
            return

        ok, status = self.__provider_set(handle, "Turning chopper on", self.__sync.set_chopper_on, True)
        self.__test_chopper_on_handle = None
        self.__record_chopper_on_result(ok, status)
        if not ok:
            handle.fail(status)
            return

        self.__refresh_test_active()
        handle.ret(OP_OK + b": " + status)

    def __execute_test_chopper_off_setters(self):
        print("Executing test chopper off setters")
        handle = self.__test_chopper_off_handle
        if handle is None:
            return

        ok, status = self.__provider_set(handle, "Turning chopper off", self.__sync.set_chopper_on, False)
        self.__test_chopper_off_handle = None
        if not ok:
            handle.fail(status)
            return

        self.__refresh_test_active()
        handle.ret(OP_OK + b": " + status)

    def __execute_test_set_phase_setters(self, phase: float):
        handle = self.__test_set_phase_handle
        if handle is None:
            return

        ok, status = self.__provider_set(handle, "Setting manual phase", self.__sync.set_target_phase, phase)
        self.__test_set_phase_handle = None
        if not ok:
            handle.fail(status)
            return

        self.__refresh_test_active()
        handle.ret(OP_OK + b": " + status)

    def __execute_do_timed_exposure(self, expose_time: float):
        handle = self.__do_timed_exposure_handle
        if handle is None:
            return

        self.__do_timed_exposure_handle = None
        try:
            ok, status = self.__provider_set(handle, "Running timed exposure", self.__sync.do_single_shot, self.__preinit_phase, self.__target_phase, expose_time)
        except Exception as exc:
            handle.fail(self.__to_bytes(f"Timed exposure failed: {exc}"))
            return

        if not ok:
            handle.fail(status)
            return

        handle.ret(OP_OK + b": " + status)

    def __execute_do_continuous_exposure(self):
        handle = self.__do_continuous_exposure_handle
        if handle is None:
            return

        self.__do_continuous_exposure_handle = None
        try:
            ok, status = self.__provider_set(handle, "Starting continuous exposure", self.__sync.set_target_phase, self.__target_phase)
        except Exception as exc:
            handle.fail(self.__to_bytes(f"Continuous exposure failed: {exc}"))
            return

        if not ok:
            handle.fail(status)
            return

        handle.ret(OP_OK + b": " + status)

    def __execute_laser_shut(self):
        handle = self.__laser_shut_handle
        if handle is None:
            return

        self.__laser_shut_handle = None
        try:
            ok, status = self.__provider_set(handle, "Shutting laser", self.__sync.set_target_phase, self.__preinit_phase)
        except Exception as exc:
            handle.fail(self.__to_bytes(f"Laser shut failed: {exc}"))
            return

        if not ok:
            handle.fail(status)
            return

        handle.ret(OP_OK + b": " + status)

    def __execute_interlock_close(self):
        try:
            ok, status = self.__sync.set_target_phase(self.__preinit_phase)
            if ok:
                self.__interlock_closed = True
                self.__logger.log(
                    "Acquisition interlock closed the sample.",
                    level="WARNING",
                    l_type="CTRL",
                    subsystem="Laser Sync Controller",
                    event="acquisition_interlock_close",
                )
            else:
                self.__logger.log(
                    f"Acquisition interlock could not close the sample: {status}",
                    level="ERROR",
                    l_type="CTRL",
                    subsystem="Laser Sync Controller",
                )
        finally:
            self.__interlock_close_pending = False

    def __execute_interlock_reopen(self):
        try:
            ok, status = self.__sync.set_target_phase(self.__target_phase)
            if ok:
                self.__interlock_closed = False
                self.__logger.log(
                    "Acquisition interlock reopened the sample after target motion resumed.",
                    level="INFO",
                    l_type="CTRL",
                    subsystem="Laser Sync Controller",
                    event="acquisition_interlock_reopen",
                )
            else:
                self.__logger.log(
                    f"Acquisition interlock could not reopen the sample: {status}",
                    level="ERROR",
                    l_type="CTRL",
                    subsystem="Laser Sync Controller",
                )
        finally:
            self.__interlock_reopen_pending = False

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

            if self.__preinit_handle is not None and not self.__preinit_setup_pending:
                if not self.__sync.get_laser_on() and self.__sync.get_chopper_on():
                    self.__preinit_setup_pending = True
                    self.__enqueue_setter_job("exp_preinit_laser")
                elif self.__preinit_ready():
                    self._on_did_preinit(b"Preinit complete.")
                    self.__preinit_handle = None

            if self.__start_handle is not None and not self.__start_setup_pending and self.__init_ready():
                self._on_did_start(b"Target phase reached.")
                self.__start_handle = None

            if self.__stop_handle is not None:
                if not self.__stop_setup_pending and not self.__sync.get_laser_on():
                    self._on_did_stop(b"Laser disabled.")
                    self.__stop_handle = None
                    self.__experiment_active = False

            if self.__test_preinit_handle is not None and not self.__test_preinit_setup_pending:
                if not self.__sync.get_laser_on() and self.__sync.get_chopper_on():
                    self.__test_preinit_setup_pending = True
                    self.__enqueue_setter_job("test_preinit_laser")
                elif self.__preinit_ready():
                    self.__test_preinit_handle.ret(OP_OK + b": test preinit complete.")
                    self.__test_preinit_handle = None

            if self.__test_init_handle is not None and not self.__test_init_setup_pending and self.__init_ready():
                self.__test_init_handle.ret(OP_OK + b": test init complete.")
                self.__test_init_handle = None

            if self.__test_stop_handle is not None:
                if not self.__test_stop_setup_pending and not self.__sync.get_laser_on():
                    self.__test_stop_handle.ret(OP_OK + b": test stop complete.")
                    self.__test_stop_handle = None
                    self.__test_active = False

            if self.__status_publisher is not None and (time.monotonic() - last_status_publish) > 0.1:
                last_status_publish = time.monotonic()
                self.__update_acquisition_interlock()
                self.__status_publisher.value = self.__get_status().encode()
                if self.__timing_status_publisher is not None:
                    self.__timing_status_publisher.value = self.__get_timing_status(
                        sampled_at_unix_ns=time.time_ns(),
                        sampled_at_monotonic_ns=time.monotonic_ns(),
                    ).encode()
                self.__update_status_items()

    def __get_status(self) -> LaserSyncStatus:
        provider_status = self.__get_provider_status()
        return LaserSyncStatus(
            laser_on=provider_status.laser_on if provider_status is not None else self.__sync.get_laser_on(),
            laser_warming_up=self.__sync.get_laser_warming_up(),
            chopper_on=provider_status.chopper_on if provider_status is not None else self.__sync.get_chopper_on(),
            chopper_starting_up=provider_status.chopper_starting_up if provider_status is not None else self.__sync.get_chopper_starting_up(),
            current_phase=self.__sync.get_current_phase(),
            target_phase=self.__sync.get_target_phase(),
            preinit_phase=self.__preinit_phase,
            configured_target_phase=self.__target_phase,
            chopper_frequency_hz=provider_status.measured_chopper_frequency_hz if provider_status is not None else self.__sync.get_chopper_frequency_hz(),
            desired_laser_on=provider_status.desired_laser_on if provider_status is not None else None,
            desired_chopper_on=provider_status.desired_chopper_on if provider_status is not None else None,
            chopper_spinning=provider_status.chopper_spinning if provider_status is not None else None,
            target_chopper_frequency_hz=provider_status.target_chopper_frequency_hz if provider_status is not None else None,
            chopper_connected=provider_status.chopper_connected if provider_status is not None else None,
            waveform_connected=provider_status.waveform_connected if provider_status is not None else None,
            chopper_recovery_exhausted=provider_status.chopper_recovery_exhausted if provider_status is not None else False,
            chopper_error=provider_status.chopper_error if provider_status is not None else None,
            waveform_error=provider_status.waveform_error if provider_status is not None else None,
        )

    def __get_timing_status(
        self,
        *,
        sampled_at_unix_ns: int | None = None,
        sampled_at_monotonic_ns: int | None = None,
    ) -> LaserTimingState:
        provider_status = self.__get_provider_status()
        return LaserTimingState(
            laser_on=provider_status.laser_on if provider_status is not None else self.__sync.get_laser_on(),
            laser_warming_up=self.__sync.get_laser_warming_up(),
            chopper_on=provider_status.chopper_on if provider_status is not None else self.__sync.get_chopper_on(),
            chopper_starting_up=provider_status.chopper_starting_up if provider_status is not None else self.__sync.get_chopper_starting_up(),
            current_phase=self.__sync.get_current_phase(),
            preinit_phase=self.__preinit_phase,
            configured_target_phase=self.__target_phase,
            chopper_frequency_hz=provider_status.measured_chopper_frequency_hz if provider_status is not None else self.__sync.get_chopper_frequency_hz(),
            sampled_at_unix_ns=sampled_at_unix_ns,
            sampled_at_monotonic_ns=sampled_at_monotonic_ns,
        )

    def __on_acquisition_health(self, payload: bytes) -> None:
        try:
            health = AcquisitionHealth.decode(payload)
        except ValueError:
            return
        self.__acquisition_health = health
        self.__acquisition_health_received_at = time.monotonic()

    def __on_target_status(self, payload) -> None:
        try:
            value = payload[0] if isinstance(payload, (tuple, list)) else payload
            self.__target_status = pickle.loads(bytes(value))
        except (IndexError, TypeError, ValueError, pickle.PickleError):
            self.__target_status = None

    def __target_motion_is_running(self) -> bool:
        return bool(getattr(self.__target_status, "is_running", False))

    def __update_acquisition_interlock(self) -> None:
        if not self.__experiment_active or self.__stop_handle is not None:
            return
        health = self.__acquisition_health
        stale = (
            self.__acquisition_health_received_at > 0
            and time.monotonic() - self.__acquisition_health_received_at > 1.0
        )
        if stale or (health is not None and health.pulse_loss):
            if not self.__interlock_closed and not self.__interlock_close_pending:
                self.__interlock_close_pending = True
                self.__enqueue_setter_job("interlock_close")
            return
        if (
            health is not None
            and health.recovery_ready
            and health.resume_authorized
            and self.__interlock_closed
            and self.__target_motion_is_running()
            and not self.__interlock_reopen_pending
        ):
            self.__interlock_reopen_pending = True
            self.__enqueue_setter_job("interlock_reopen")

    def _on_preinit(self, handle):
        self.__experiment_active = True
        self.__acquisition_health_received_at = time.monotonic()
        self.__interlock_closed = False
        self.__interlock_close_pending = False
        self.__interlock_reopen_pending = False
        self.__preinit_handle = handle
        self.__preinit_setup_pending = True
        self.__enqueue_setter_job("exp_preinit")

        self.__logger.log(
            f"Laser preinit started (phase={self.__preinit_phase}).",
            level="INFO",
            l_type="CTRL",
            subsystem="Laser Sync Controller",
            event="preinit_laser",
        )

        return True, b": preinit accepted, configuring laser/chopper."

    def _on_start(self, handle):
        self.__start_handle = handle
        self.__start_setup_pending = True
        self.__enqueue_setter_job("exp_start")

        self.__logger.log(
            f"Laser init started (target phase={self.__target_phase}).",
            level="INFO",
            l_type="CTRL",
            subsystem="Laser Sync Controller",
            event="init_laser",
        )

        return True, b": init accepted, configuring target phase."

    def _on_stop(self, handle):
        self.__stop_handle = handle
        self.__stop_setup_pending = True
        self.__enqueue_setter_job("exp_stop")

        self.__logger.log(
            "Laser stop requested.",
            level="INFO",
            l_type="CTRL",
            subsystem="Laser Sync Controller",
            event="stop_laser",
        )

        return True, b": stop accepted, disabling laser/chopper."

    def _on_continue_state(self):
        if self.__experiment_active and self.__acquisition_health_received_at and time.monotonic() - self.__acquisition_health_received_at > 1.0:
            return False, b"Acquisition health is stale."
        if self.__experiment_active or self.__preinit_handle is not None or self.__start_handle is not None or self.__stop_handle is not None:
            return True, self.EXP_IN_PROGRESS

        return False, b"Laser/chopper are not active."

    def _can_preinit(self, settings, state):
        frequency = settings.get_chopper_frequency_hz()
        try:
            frequency = float(frequency)
        except (TypeError, ValueError):
            return False, b"Exposure requires a configured chopper frequency."
        if not frequency > 0:
            return False, b"Exposure chopper frequency must be positive."
        if self.__test_active or self.__test_in_progress() or self.__exposure_control_in_progress():
            return False, b"Laser test control is active."

        self.__requested_chopper_frequency_hz = frequency
        return super()._can_preinit(settings, state)

    def __test_in_progress(self) -> bool:
        return (
            self.__test_preinit_handle is not None
            or self.__test_init_handle is not None
            or self.__test_stop_handle is not None
            or self.__test_laser_on_handle is not None
            or self.__test_laser_off_handle is not None
            or self.__test_chopper_on_handle is not None
            or self.__test_chopper_off_handle is not None
            or self.__test_set_phase_handle is not None
        )

    def __exposure_control_in_progress(self) -> bool:
        return (
            self.__do_timed_exposure_handle is not None
            or self.__do_continuous_exposure_handle is not None
            or self.__laser_shut_handle is not None
        )

    def __refresh_test_active(self):
        self.__test_active = (
            not self.__experiment_active
            and (self.__sync.get_laser_on() or self.__sync.get_chopper_on() or self.__test_in_progress())
        )

    def __on_test_preinit(self, _s_uuid, _param, handle: client._EventHandler._IncomingEventHandle):
        self.__test_active = True
        self.__test_preinit_handle = handle
        self.__test_preinit_setup_pending = True
        self.__enqueue_setter_job("test_preinit")
        handle.feedback(magics.OP_IN_PROGRESS + b": test preinit started.")

    def __on_test_init(self, _s_uuid, _param, handle: client._EventHandler._IncomingEventHandle):
        self.__test_active = True
        self.__test_init_handle = handle
        self.__test_init_setup_pending = True
        self.__enqueue_setter_job("test_init")
        handle.feedback(magics.OP_IN_PROGRESS + b": test init started.")

    def __on_test_stop(self, _s_uuid, _param, handle: client._EventHandler._IncomingEventHandle):
        self.__test_stop_handle = handle
        self.__test_stop_setup_pending = True
        self.__enqueue_setter_job("test_stop")
        handle.feedback(magics.OP_IN_PROGRESS + b": test stop started.")

    def __on_test_laser_on(self, _s_uuid, _param, handle: client._EventHandler._IncomingEventHandle):
        self.__test_laser_on_handle = handle
        self.__enqueue_setter_job("test_laser_on")
        handle.feedback(magics.OP_IN_PROGRESS + b": manual laser on started.")

    def __on_test_laser_off(self, _s_uuid, _param, handle: client._EventHandler._IncomingEventHandle):
        self.__test_laser_off_handle = handle
        self.__enqueue_setter_job("test_laser_off")
        handle.feedback(magics.OP_IN_PROGRESS + b": manual laser off started.")

    def __on_test_chopper_on(self, _s_uuid, _param, handle: client._EventHandler._IncomingEventHandle):
        self.__test_chopper_on_handle = handle
        self.__enqueue_setter_job("test_chopper_on")
        handle.feedback(magics.OP_IN_PROGRESS + b": manual chopper on started.")

    def __on_test_chopper_off(self, _s_uuid, _param, handle: client._EventHandler._IncomingEventHandle):
        self.__test_chopper_off_handle = handle
        self.__enqueue_setter_job("test_chopper_off")
        handle.feedback(magics.OP_IN_PROGRESS + b": manual chopper off started.")

    def __on_test_set_phase(self, _s_uuid, param, handle: client._EventHandler._IncomingEventHandle):
        self.__test_set_phase_handle = handle
        try:
            phase = float(pickle.loads(param))
        except (TypeError, ValueError, pickle.UnpicklingError):
            self.__test_set_phase_handle = None
            handle.fail(b"Invalid phase payload.")
            return

        self.__enqueue_setter_job("test_set_phase", phase)
        handle.feedback(magics.OP_IN_PROGRESS + b": manual phase set started.")

    def __on_do_timed_exposure(self, _s_uuid, param, handle: client._EventHandler._IncomingEventHandle):
        if self.__preinit_handle is not None or self.__start_handle is not None or self.__stop_handle is not None:
            handle.fail(b"Cannot perform timed exposure during laser lifecycle transitions.")
            return

        if self.__test_in_progress() or self.__exposure_control_in_progress():
            handle.fail(b"Laser control already in progress.")
            return

        self.__do_timed_exposure_handle = handle
        try:
            expose_time = struct.unpack('d', param)[0]
        except (struct.error, IndexError):
            self.__do_timed_exposure_handle = None
            handle.fail(b"Invalid exposure time payload.")
            return

        if expose_time < 0:
            self.__do_timed_exposure_handle = None
            handle.fail(b"Exposure time must be non-negative.")
            return

        self.__enqueue_setter_job("do_timed_exposure", expose_time)
        handle.feedback(magics.OP_IN_PROGRESS + b": timed exposure started.")

    def __on_do_continuous_exposure(self, _s_uuid, _param, handle: client._EventHandler._IncomingEventHandle):
        if self.__preinit_handle is not None or self.__start_handle is not None or self.__stop_handle is not None:
            handle.fail(b"Cannot perform continuous exposure during laser lifecycle transitions.")
            return

        if self.__test_in_progress() or self.__exposure_control_in_progress():
            handle.fail(b"Laser control already in progress.")
            return

        self.__do_continuous_exposure_handle = handle
        self.__enqueue_setter_job("do_continuous_exposure")
        handle.feedback(magics.OP_IN_PROGRESS + b": continuous exposure started.")

    def __on_laser_shut(self, _s_uuid, _param, handle: client._EventHandler._IncomingEventHandle):
        if self.__preinit_handle is not None or self.__start_handle is not None or self.__stop_handle is not None:
            handle.fail(b"Cannot shut laser during lifecycle transitions.")
            return

        if self.__test_in_progress() or self.__exposure_control_in_progress():
            handle.fail(b"Laser control already in progress.")
            return

        self.__laser_shut_handle = handle
        self.__enqueue_setter_job("laser_shut")
        handle.feedback(magics.OP_IN_PROGRESS + b": laser shut started.")

    def __on_preinit_phase_write(self, _h, _requester, value: float):
        self.__preinit_phase = value
        self.__request_config_save()
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
        self.__request_config_save()
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

    def __on_initial_phase_write(self, _h, _requester, value: float):
        self.__initial_phase = value
        ok, status = self.__sync.set_initial_phase(value)
        if not ok:
            return (magics.TRANSOP_STATE_REJ, self.__to_bytes(status))
        if not self.__sync.get_laser_on():
            ok, status = self.__reset_to_initial_phase()
            if not ok:
                return (magics.TRANSOP_STATE_REJ, status)
        self.__request_config_save()
        self.__logger.log(
            f"Updated initial phase to {value}.",
            level="DEBUG",
            l_type="CTRL",
            subsystem="Laser Sync Controller",
            event="set_initial_phase",
        )
        return (magics.TRANSOP_STATE_OK, OP_OK)

    def __on_initial_phase_read(self, _requester):
        return (magics.TRANSOP_STATE_OK, self.__initial_phase)

    def __on_skew_rate_write(self, _h, _requester, value: float):
        ok, status = self.__sync.set_skew_rate(value)
        if not ok:
            return (magics.TRANSOP_STATE_REJ, self.__to_bytes(status))
        self.__skew_rate = value
        self.__request_config_save()
        self.__logger.log(
            f"Updated skew rate to {value}.",
            level="DEBUG",
            l_type="CTRL",
            subsystem="Laser Sync Controller",
            event="set_skew_rate",
        )
        return (magics.TRANSOP_STATE_OK, OP_OK)

    def __on_skew_rate_read(self, _requester):
        return (magics.TRANSOP_STATE_OK, self.__skew_rate)

    def __on_laser_warmup_time_write(self, _h, _requester, value: float):
        ok, status = self.__sync.set_laser_warmup_time(value)
        if not ok:
            return (magics.TRANSOP_STATE_REJ, self.__to_bytes(status))
        self.__laser_warmup_time = value
        self.__request_config_save()
        self.__logger.log(
            f"Updated laser warmup time to {value}.",
            level="DEBUG",
            l_type="CTRL",
            subsystem="Laser Sync Controller",
            event="set_laser_warmup_time",
        )
        return (magics.TRANSOP_STATE_OK, OP_OK)

    def __on_laser_warmup_time_read(self, _requester):
        return (magics.TRANSOP_STATE_OK, self.__laser_warmup_time)

    def __on_chopper_startup_time_write(self, _h, _requester, value: float):
        ok, status = self.__sync.set_chopper_startup_time(value)
        if not ok:
            return (magics.TRANSOP_STATE_REJ, self.__to_bytes(status))
        self.__chopper_startup_time = value
        self.__request_config_save()
        self.__logger.log(
            f"Updated chopper startup time to {value}.",
            level="DEBUG",
            l_type="CTRL",
            subsystem="Laser Sync Controller",
            event="set_chopper_startup_time",
        )
        return (magics.TRANSOP_STATE_OK, OP_OK)

    def __on_chopper_startup_time_read(self, _requester):
        return (magics.TRANSOP_STATE_OK, self.__chopper_startup_time)

    def __on_chopper_frequency_write(self, _h, _requester, value: float):
        if (
            self.__experiment_active
            or self.__preinit_handle is not None
            or self.__start_handle is not None
            or self.__stop_handle is not None
        ):
            return (magics.TRANSOP_STATE_REJ, b"Cannot change chopper target during an active exposure.")
        ok, status = self.__sync.set_chopper_frequency_hz(value)
        if not ok:
            return (magics.TRANSOP_STATE_REJ, self.__to_bytes(status))
        self.__logger.log(
            f"Updated chopper target frequency to {float(value):.0f} Hz.",
            level="INFO",
            l_type="CTRL",
            subsystem="Laser Sync Controller",
            event="set_chopper_frequency",
        )
        return (magics.TRANSOP_STATE_OK, OP_OK)

    def __on_chopper_frequency_read(self, _requester):
        target_hz = self.__sync.get_hardware_status().target_chopper_frequency_hz
        if target_hz is None:
            return (magics.TRANSOP_STATE_REJ, b"Chopper target frequency is unavailable.")
        return (magics.TRANSOP_STATE_OK, float(target_hz))

    def __on_got_subsystem(self, handle: client._RegisteredSubsystemHandle):
        self.__subsystem = handle

        handle.add_event_handler(b"laser_test_preinit").on_called(self.__on_test_preinit)
        handle.add_event_handler(b"laser_test_init").on_called(self.__on_test_init)
        handle.add_event_handler(b"laser_test_stop").on_called(self.__on_test_stop)
        handle.add_event_handler(b"laser_test_laser_on").on_called(self.__on_test_laser_on)
        handle.add_event_handler(b"laser_test_laser_off").on_called(self.__on_test_laser_off)
        handle.add_event_handler(b"laser_test_chopper_on").on_called(self.__on_test_chopper_on)
        handle.add_event_handler(b"laser_test_chopper_off").on_called(self.__on_test_chopper_off)
        handle.add_event_handler(b"laser_test_set_phase").on_called(self.__on_test_set_phase)

        handle.add_event_handler(b"laser_do_timed_exposure").on_called(self.__on_do_timed_exposure)
        handle.add_event_handler(b"laser_do_continuous_exposure").on_called(self.__on_do_continuous_exposure)
        handle.add_event_handler(b"laser_shut").on_called(self.__on_laser_shut)

        self.__status_publisher = handle.get_kv_property(b"status", False, True, True)
        self.__status_publisher.set_type(types.ByteTypeSpecifier())
        self.__status_publisher.value = self.__get_status().encode()
        self.__timing_status_publisher = handle.get_kv_property(b"timing_status", False, True, True)
        self.__timing_status_publisher.set_type(types.ByteTypeSpecifier())
        self.__timing_status_publisher.value = self.__get_timing_status(
            sampled_at_unix_ns=time.time_ns(),
            sampled_at_monotonic_ns=time.monotonic_ns(),
        ).encode()
        acquisition_health_kv = handle.add_remote_kv(
            uuids.UUID_EUV_ACQUISITION_CONTROLLER,
            dds_subsystem.KVDescriptor(types.ByteTypeSpecifier(), b"acquisition_health", True, True, False),
        )
        acquisition_health_kv.on_new_data_received(self.__on_acquisition_health)
        target_status_kv = handle.add_remote_kv(
            uuids.UUID_TARGET_CONTROLLER,
            dds_subsystem.KVDescriptor(types.VectorTypeSpecifier(types.ByteTypeSpecifier(), 2), b"status", True, True, False),
        )
        target_status_kv.on_new_data_received(self.__on_target_status)

        preinit_phase_kv = handle.add_kv_handler(b"preinit_phase")
        preinit_phase_kv.set_type(types.FloatTypeSpecifier())
        preinit_phase_kv.on_set(self.__on_preinit_phase_write)
        preinit_phase_kv.on_get(self.__on_preinit_phase_read)

        target_phase_kv = handle.add_kv_handler(b"target_phase")
        target_phase_kv.set_type(types.FloatTypeSpecifier())
        target_phase_kv.on_set(self.__on_target_phase_write)
        target_phase_kv.on_get(self.__on_target_phase_read)

        initial_phase_kv = handle.add_kv_handler(b"initial_phase")
        initial_phase_kv.set_type(types.FloatTypeSpecifier())
        initial_phase_kv.on_set(self.__on_initial_phase_write)
        initial_phase_kv.on_get(self.__on_initial_phase_read)

        skew_rate_kv = handle.add_kv_handler(b"skew_rate")
        skew_rate_kv.set_type(types.FloatTypeSpecifier())
        skew_rate_kv.on_set(self.__on_skew_rate_write)
        skew_rate_kv.on_get(self.__on_skew_rate_read)

        laser_warmup_kv = handle.add_kv_handler(b"laser_warmup_time")
        laser_warmup_kv.set_type(types.FloatTypeSpecifier())
        laser_warmup_kv.on_set(self.__on_laser_warmup_time_write)
        laser_warmup_kv.on_get(self.__on_laser_warmup_time_read)

        chopper_startup_kv = handle.add_kv_handler(b"chopper_startup_time")
        chopper_startup_kv.set_type(types.FloatTypeSpecifier())
        chopper_startup_kv.on_set(self.__on_chopper_startup_time_write)
        chopper_startup_kv.on_get(self.__on_chopper_startup_time_read)

        chopper_frequency_kv = handle.add_kv_handler(b"chopper_frequency_hz")
        chopper_frequency_kv.set_type(types.FloatTypeSpecifier())
        chopper_frequency_kv.on_set(self.__on_chopper_frequency_write)
        chopper_frequency_kv.on_get(self.__on_chopper_frequency_read)

        self._setup_subsystem(handle)

    def ok(self):
        return self.__run and self.__client.ok() and self.__daemon.is_ok()

    def close(self):
        self.__request_config_save()

        begin = time.monotonic()
        while (not self.__config_save_queue.empty() or self.__config_save_in_progress) and (time.monotonic() - begin) < 2.0:
            time.sleep(0.05)

        self.__daemon.stop()
        self.__sync.stop()

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