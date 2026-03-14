
import sys

import win32serviceutil
import win32service
import win32event
import servicemanager

from ipi_ecs.services.lifecycle_manager_service import LifecycleManagerService
from chamber_ctl.subsystems import exposure_controller, laser, oscilloscope, target_controller
from chamber_ctl.subsystems import uuids

class ChamberLifecycleManagerService(LifecycleManagerService):
    def __init__(self, args):
        super().__init__(args, uuid=uuids.UUID_LIFECYCLE_MANAGER)
        
    _svc_name_ = "chamber-ctl-LifecycleManagerService"
    _svc_display_name_ = "chamber-ctl Lifecycle Manager Service"
    _svc_description_ = "Lifecycle Manager Service for Chamber Control"
    def add_subsystems(self):
        # Add chamber-specific subsystems here.
        self._lifecycle_manager.add_subsystem(uuids.UUID_EXPOSURE_CONTROLLER, exposure_controller.main)
        self._lifecycle_manager.add_subsystem(uuids.UUID_OSCILLOSCOPE_CONTROLLER, oscilloscope.main)
        self._lifecycle_manager.add_subsystem(uuids.UUID_TARGET_CONTROLLER, target_controller.main)
        self._lifecycle_manager.add_subsystem(uuids.UUID_LASER_CONTROLLER, laser.main)

def main():
    if len(sys.argv) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(ChamberLifecycleManagerService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        # Called from command line with parameters (install, start, etc.)
        win32serviceutil.HandleCommandLine(ChamberLifecycleManagerService)

if __name__ == '__main__':
    main()