
import sys

import win32serviceutil
import win32service
import win32event
import servicemanager

from ipi_ecs.services.lifecycle_manager_service import LifecycleManagerService
from chamber_ctl.subsystems import exposure_controller
from chamber_ctl.subsystems import uuids

class ChamberLifecycleManagerService(LifecycleManagerService):
    def add_subsystems(self):
        # Add chamber-specific subsystems here.
        self._lifecycle_manager.add_subsystem(uuids.UUID_EXPOSURE_CONTROLLER, exposure_controller.main)
        print("Added ExposureController subsystem.")

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