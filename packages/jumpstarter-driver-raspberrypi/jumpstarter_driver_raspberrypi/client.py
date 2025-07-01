from dataclasses import dataclass
import click
from digitalio import DriveMode, Pull  # type: ignore

from jumpstarter_driver_power.client import PowerClient


@dataclass(kw_only=True)
class DigitalIOClient(PowerClient):
    """DigitalIO (Digital GPIO) client class

    Client methods for the DigitalIO driver.
    """

    def switch_to_output(self, value: bool = False, drive_mode: DriveMode = DriveMode.PUSH_PULL) -> None:
        """
        Switch pin to output mode with given default value and drive mode
        """

        match drive_mode:
            case DriveMode.PUSH_PULL:
                drive_mode = 0
            case DriveMode.OPEN_DRAIN:
                drive_mode = 1
            case _:
                raise ValueError("unrecognized drive_mode")
        self.call("switch_to_output", value, drive_mode)

    def switch_to_input(self, pull: Pull | None = None) -> None:
        """
        Switch pin to input mode with given pull up/down mode
        """

        match pull:
            case None:
                pull = 0
            case Pull.UP:
                pull = 1
            case Pull.DOWN:
                pull = 2
            case _:
                raise ValueError("unrecognized pull")
        self.call("switch_to_input", pull)

    @property
    def value(self) -> bool:
        """
        Current value of the pin
        """

        return self.call("get_value")

    @value.setter
    def value(self, value: bool) -> None:
        self.call("set_value", value)

    def on(self):
        """Turn power on"""
        self.call("on")

    def off(self):
        """Turn power off"""
        self.call("off")

    def cli(self):
        @click.group()
        def gpio():
            """GPIO power control commands"""
            pass

        for cmd in super().cli().commands.values():
            gpio.add_command(cmd)

        return gpio
