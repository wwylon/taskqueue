"""F-239: the UI server's listen backlog must absorb a cold page load's burst of module requests."""

from __future__ import annotations

import socket

from tqdaemon.web.responses import BoundedThreadingHTTPServer


def test_burst_of_connections_queues_instead_of_being_refused(tmp_path):
    # Not serving yet: every connection has to wait in the kernel backlog (listen(5) refused most).
    server = BoundedThreadingHTTPServer(("127.0.0.1", 0), object)
    socks: list[socket.socket] = []
    try:
        failures = 0
        for _ in range(40):
            s = socket.socket()
            s.settimeout(3)
            try:
                s.connect(server.server_address)
                socks.append(s)
            except OSError:
                failures += 1
                s.close()
        assert failures == 0
    finally:
        for s in socks:
            s.close()
        server.server_close()
