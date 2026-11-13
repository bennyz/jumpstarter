import os
import ssl
import sys

import truststore


def _inject_system_certs() -> None:
    """Make Python's TLS clients trust the system certificate store.

    truststore.inject_into_ssl() also patches urllib3 and requests, importing
    both to do so, which costs every `j` command about 50 ms. Those patches
    only matter when the libraries were imported first: urllib3 copies
    ssl.SSLContext when it is imported. If neither is loaded yet, replacing
    ssl.SSLContext is enough, and both pick it up when they are imported.
    """
    if "urllib3" in sys.modules or "requests" in sys.modules:
        truststore.inject_into_ssl()
    else:
        ssl.SSLContext = truststore.SSLContext  # ty: ignore[invalid-assignment]


# if we are running in MacOS avoid injecting system certificates to avoid
# https://github.com/jumpstarter-dev/jumpstarter/issues/362
# also allow to force the system certificates injection with
# the JUMPSTARTER_FORCE_SYSTEM_CERTS environment variable set to "1"
if os.uname().sysname != "Darwin" or os.environ.get("JUMPSTARTER_FORCE_SYSTEM_CERTS") == "1":
    _inject_system_certs()
