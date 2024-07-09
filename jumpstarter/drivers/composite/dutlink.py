from . import Composite
from .. import DriverBase
from ..power import Power, PowerReading
from dataclasses import dataclass, field
from collections.abc import Generator
from typing import List, Optional
import usb.core
import usb.util


@dataclass(kw_only=True)
class Dutlink(Composite):
    serial: Optional[str]
    devices: List[DriverBase] = field(init=False)
    dev: usb.core.Device = field(init=False)

    def __post_init__(self):
        for dev in usb.core.find(idVendor=0x2B23, idProduct=0x1012, find_all=True):
            serial = usb.util.get_string(dev, dev.iSerialNumber)
            if serial == self.serial or self.serial is None:
                self.dev = dev
                self.itf = usb.util.find_descriptor(
                    dev.get_active_configuration(),
                    bInterfaceClass=0xFF,
                    bInterfaceSubClass=0x1,
                    bInterfaceProtocol=0x1,
                )

                self.devices = [
                    DutlinkPower(
                        labels={"jumpstarter.dev/name": "power"},
                        parent=self,
                    )
                ]
                return
            raise FileNotFoundError("failed to find dutlink device")

    def control(self, direction, ty, actions, action, value):
        if direction == usb.ENDPOINT_IN:
            self.dev.ctrl_transfer(
                bmRequestType=usb.ENDPOINT_OUT | usb.TYPE_VENDOR | usb.RECIP_INTERFACE,
                wIndex=self.itf.bInterfaceNumber,
                bRequest=0x00,
            )

        op = actions.index(action)
        res = self.dev.ctrl_transfer(
            bmRequestType=direction | usb.TYPE_VENDOR | usb.RECIP_INTERFACE,
            wIndex=self.itf.bInterfaceNumber,
            bRequest=ty,
            wValue=op,
            data_or_wLength=(value if direction == usb.ENDPOINT_OUT else 512),
        )

        if direction == usb.ENDPOINT_IN:
            return bytes(res).decode("utf-8")


@dataclass(kw_only=True)
class DutlinkPower(Power):
    parent: Dutlink

    def control(self, action):
        return self.parent.control(
            usb.ENDPOINT_OUT,
            0x01,
            ["off", "on", "force-off", "force-on", "rescue"],
            action,
            None,
        )

    def on(self) -> str:
        return self.control("on")

    def off(self) -> str:
        return self.control("off")

    def read(self) -> Generator[PowerReading, None, None]:
        yield None