"""Is a TCP port free on this machine?

The API runs on the server that nginx serves the organizations' portals from, so a port another program (or
another nginx site) already listens on cannot become an organization's portal: nginx would fail to bind it, and
`nginx -s reload` still reports success. TENANT_PORT_CHECK_SERVER=false turns this off where the API runs on a
different machine than nginx.
"""

import errno
import socket


def port_in_use(port: int) -> bool:
    """True when something on this machine listens on the port (IPv4 or IPv6)."""
    for family, address in ((socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::")):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as probe:
                # Without it, connections still closing on the port (TIME_WAIT) would read as in use.
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if family == socket.AF_INET6:
                    probe.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                probe.bind((address, port))
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                return True
            # No IPv6 on this machine, or binding not allowed: this family tells us nothing.
    return False
