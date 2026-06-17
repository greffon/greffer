import socket


def _sock_type(protocol):
    return socket.SOCK_DGRAM if protocol == 'udp' else socket.SOCK_STREAM


def get_free_ports(host='127.0.0.1', numbers=1, protocol='tcp'):
    """Allocate ``numbers`` ephemeral host ports for Tier-A nginx upstreams.

    TCP and UDP are independent port namespaces, so probe with the matching
    socket type. All sockets in a batch are held open until the end so the same
    number is not handed out twice within one call.

    Tier-A only: these host ports are internal nginx upstreams, never
    user-facing. L4 (Tier-C) host ports are NOT allocated here — a ``bind()``
    inside the greffer's container network namespace is blind to host bindings,
    so the daemon-truth allocator in apps/utils/docker/l4_ports.py is used for
    L4 instead.
    """
    sock_type = _sock_type(protocol)
    socks = []
    ports = []
    for _ in range(numbers):
        sock = socket.socket(socket.AF_INET, sock_type)
        sock.bind((host, 0))
        ports.append(sock.getsockname()[1])
        socks.append(sock)
    for i in range(numbers):
        socks[i].close()
    return ports
