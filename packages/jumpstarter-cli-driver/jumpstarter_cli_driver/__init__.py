import logging
from typing import Optional

import asyncclick as click
from jumpstarter_cli_common import AliasedGroup, opt_log_level, version

from .driver import list_drivers


@click.group(cls=AliasedGroup)
@opt_log_level
def driver(log_level: Optional[str]):
    """Jumpstarter driver CLI tool"""
    if log_level:
        logging.basicConfig(level=log_level.upper())
    else:
        logging.basicConfig(level=logging.INFO)


driver.add_command(list_drivers)
driver.add_command(version)

if __name__ == "__main__":
    driver()
