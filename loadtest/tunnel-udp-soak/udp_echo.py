#!/usr/bin/env python3
"""Trivial UDP echo: reflect every datagram back to its sender.

This is the echo service the soak rig runs (``docker-compose.soak.yml``), and it
doubles as the local target for validating ``udp_soak.py`` without the relay
(point the driver at it to check the measurement logic). Stdlib-only, so it needs
no image build. ``socat UDP-LISTEN:7777,fork,reuseaddr EXEC:cat`` is an
equivalent if you prefer it, but note socat forks per datagram, which becomes the
bottleneck sooner under load.

Usage: ``python udp_echo.py [HOST] [PORT]`` (default 127.0.0.1 7777).
"""

import socket
import sys


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    host = argv[0] if len(argv) > 0 else '127.0.0.1'
    port = int(argv[1]) if len(argv) > 1 else 7777
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((host, port))
    sys.stderr.write('udp-echo listening on %s:%d\n' % (host, port))
    sys.stderr.flush()
    while True:
        data, addr = sock.recvfrom(65535)
        sock.sendto(data, addr)


if __name__ == '__main__':
    main()
