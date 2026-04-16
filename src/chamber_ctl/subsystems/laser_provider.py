import abc


class LaserSyncProvider(abc.ABC):
    @abc.abstractmethod
    def set_target_phase(self, phase: float) -> tuple[bool, str]:
        pass

    @abc.abstractmethod
    def get_target_phase(self) -> float:
        pass

    @abc.abstractmethod
    def set_current_phase(self, phase: float) -> tuple[bool, str]:
        pass

    @abc.abstractmethod
    def get_current_phase(self) -> float:
        pass

    @abc.abstractmethod
    def set_initial_phase(self, phase: float) -> tuple[bool, str]:
        pass

    @abc.abstractmethod
    def get_initial_phase(self) -> float:
        pass

    @abc.abstractmethod
    def set_chopper_on(self, on: bool) -> tuple[bool, str]:
        pass

    @abc.abstractmethod
    def get_chopper_on(self) -> bool:
        pass

    @abc.abstractmethod
    def get_chopper_starting_up(self) -> bool:
        pass

    @abc.abstractmethod
    def set_laser_on(self, on: bool) -> tuple[bool, str]:
        pass

    @abc.abstractmethod
    def get_laser_on(self) -> bool:
        pass

    @abc.abstractmethod
    def get_laser_warming_up(self) -> bool:
        pass

    @abc.abstractmethod
    def set_skew_rate(self, skew_rate: float) -> tuple[bool, str]:
        pass

    @abc.abstractmethod
    def get_skew_rate(self) -> float:
        pass

    @abc.abstractmethod
    def set_laser_warmup_time(self, warmup_time: float) -> tuple[bool, str]:
        pass

    @abc.abstractmethod
    def get_laser_warmup_time(self) -> float:
        pass

    @abc.abstractmethod
    def set_chopper_startup_time(self, startup_time: float) -> tuple[bool, str]:
        pass

    @abc.abstractmethod
    def get_chopper_startup_time(self) -> float:
        pass

    @abc.abstractmethod
    def do_single_shot(self, expose_time: float) -> tuple[bool, str]:
        pass

    @abc.abstractmethod
    def start(self):
        pass

    @abc.abstractmethod
    def stop(self):
        pass
