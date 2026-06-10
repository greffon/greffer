#!/usr/bin/env python3
"""Trivial UDP echo: reflect every datagram back to its sender.

The soak rig normally uses ``socat UDP-LISTEN:...,fork EXEC:cat`` as the echo
service (matching the catalog-style tunnel L4 app). This stdlib echo is the
zero-dependency equivalent, used to validate ``udp_soak.py`` locally without the
relay (point the driver at this echo to check the measurement logic) and as a
fallback echo on hosts without socat.

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
