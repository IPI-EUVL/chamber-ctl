import multiprocessing
import time
import uuid
import sys

import ipi_ecs.dds.subsystem as subsystem
import ipi_ecs.dds.types as types
import ipi_ecs.dds.client as client
import ipi_ecs.dds.magics as magics
import ipi_ecs.core.tcp as tcp

from ipi_ecs.logging.client import LogClient

from chamber_ctl.subsystems import uuids

class ExposureSettings:
    target_dose = 0.0
    target_time = 0.0

    def encode(self):
        return pickle.dumps(self)
    
    @staticmethod
    def decode(b_data: bytes):
        return pickle.loads(b_data)
    
class ExposureState:
    def __init__(self, exposure_settings: ExposureSettings):
        self.exposure_settings = exposure_settings
        self.__uuid = uuid.uuid4()


class ExposureController:
    def __init__(self):
        self.__run = True

        c_uuid = uuid.uuid4()

        self.__logger_sock = tcp.TCPClientSocket()

        self.__logger_sock.connect(("127.0.0.1", 11751))
        self.__logger_sock.start()

        self.__logger = LogClient(self.__logger_sock, origin_uuid=c_uuid)

        self.__did_config = False
        self.__subsystem = None

        def _on_ready():
            if self.__did_config:
                return
            
            self.__did_config = True
            sh = self.__client.register_subsystem("Exposure Controller", UUID_EXPOSURE_CONTROLLER)

            self.__on_got_subsystem(sh)

        #print("Registering subsystem...")
        self.__client = client.DDSClient(c_uuid, logger=self.__logger)
        self.__client.when_ready().then(_on_ready)
        

    def __on_got_subsystem(self, handle: client._RegisteredSubsystemHandle):
        # state has to be named state as it will get passed as a KW argument, stop complaining Pylint!!
        def fail(state, reason): #pylint: disable=unused-argument
            print(f"Failed: {reason}")
            self.__run = False

        self.__subsystem = handle

    def ok(self):
        return self.__run and self.__client.ok()
    
    def close(self):
        self.__client.close()
        self.__logger_sock.close()

        self.__run = False

def main(stop_event: "multiprocessing.Event"):
    m_exposure_controller = ExposureController()

    try:
        while m_exposure_controller.ok() and not (stop_event is not None and stop_event.is_set()):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        m_exposure_controller.close()

if __name__ == "__main__":
    main(None)