import multiprocessing
import uuid
import time

from ipi_ecs.subsystems.lifecycle_manager import LifecycleManager

from chamber_ctl.subsystems import batch_controller, euv_acquisition_controller, exposure_controller, target_controller, laser, sample_motion, experiment_sequencer, development_metrics_bridge
import chamber_ctl.subsystems.uuids as uuids


def main(stop_event: "multiprocessing.Event"):
    m_client = LifecycleManager(uuids.UUID_LIFECYCLE_MANAGER)
    m_client.add_subsystem(uuids.UUID_EXPOSURE_CONTROLLER, exposure_controller.main)
    m_client.add_subsystem(uuids.UUID_TARGET_CONTROLLER, target_controller.main)
    m_client.add_subsystem(uuids.UUID_EUV_ACQUISITION_CONTROLLER, euv_acquisition_controller.main)
    m_client.add_subsystem(uuids.UUID_LASER_CONTROLLER, laser.main)
    m_client.add_subsystem(uuids.UUID_SAMPLE_MOTION_CONTROLLER, sample_motion.main)
    m_client.add_subsystem(uuids.UUID_EXPERIMENT_QUEUE_CONTROLLER, experiment_sequencer.main)
    m_client.add_subsystem(uuids.UUID_EXPOSURE_BATCH_CONTROLLER, batch_controller.main)
    m_client.add_subsystem(uuids.UUID_DEVELOPMENT_METRICS_CONTROLLER, development_metrics_bridge.main)

    try:
        while m_client.ok() and not (stop_event is not None and stop_event.is_set()):
            time.sleep(1)

    except KeyboardInterrupt:
        pass
    finally:
        m_client.close()

    return 0


if __name__ == "__main__":
    main(None)
