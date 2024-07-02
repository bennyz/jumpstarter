from abc import abstractmethod
from dataclasses import dataclass
from ..base import DriverBase


@dataclass
class PowerReading:
    voltage: float
    current: float
    apparent_power: float

    def __init__(self, voltage: float, current: float):
        self.voltage = voltage
        self.current = current
        self.apparent_power = voltage * current

    def __repr__(self):
        return f"<PowerReading: {self.voltage}V {self.current}A {self.apparent_power}W>"


class Power(DriverBase):
    @abstractmethod
    def on(self): ...

    @abstractmethod
    def off(self): ...

    @abstractmethod
    def read(self) -> PowerReading: ...