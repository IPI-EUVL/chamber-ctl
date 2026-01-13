import time
import uuid
import sys
from multiprocessing import Event

import ipi_ecs.dds.subsystem as subsystem
import ipi_ecs.dds.types as types
import ipi_ecs.dds.client as client
import ipi_ecs.dds.magics as magics
import ipi_ecs.core.tcp as tcp

from ipi_ecs.logging.client import LogClient

def main(stop_event: Event):
    c_uuid = uuid.uuid4()

    sock = tcp.TCPClientSocket()

    sock.connect(("127.0.0.1", 11751))
    sock.start()

    logger = LogClient(sock, origin_uuid=c_uuid)

    m_client = client.DDSClient(c_uuid, logger=logger)
    s = None

    def reg_s():
        s = m_client.register_subsystem("Exposure Controller", uuid.uuid3(uuid.NAMESPACE_OID, "1"))

    m_client.when_ready().then(reg_s)
    time.sleep(1)

    try:
        while m_client.ok() and not stop_event.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        m_client.close()
        time.sleep(0.1)
        sock.close()