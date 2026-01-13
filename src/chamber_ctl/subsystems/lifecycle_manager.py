import argparse
import uuid
import time

from ipi_ecs.dds.lifecycle_manager import LifecycleManager

from chamber_ctl.subsystems import exposure_controller

def main(args: argparse.Namespace):
    m_client = LifecycleManager(uuid.uuid3(uuid.NAMESPACE_OID, "lifecycle_manager"))
    m_client.add_subsystem(
        uuid.uuid3(uuid.NAMESPACE_OID, "1"), exposure_controller.main
    )

    try:
        while m_client.ok():
            time.sleep(1)
            states = m_client.get_states()
            if states is None:
                print("Could not get states.")
                continue

    except KeyboardInterrupt:
        pass
    finally:
        m_client.close()

    return 0


if __name__ == "__main__":
    main(None)