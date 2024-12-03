from dataclasses import dataclass

import asyncclick as click

from jumpstarter.client import DriverClient


@dataclass(kw_only=True)
class CompositeClient(DriverClient):
    def __getattr__(self, name):
        return self.children[name]

    def cli(self):
        @click.group
        def base():
            """Generic composite device"""
            pass

        for k, v in self.children.items():
            if hasattr(v, "cli"):
                base.add_command(v.cli(), k)

        return base
