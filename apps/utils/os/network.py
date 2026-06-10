import socket


def _sock_type(protocol):
    return socket.SOCK_DGRAM if protocol == 'udp' else socket.SOCK_STREAM


def _maybe_reuseaddr(sock, protocol):
    # SO_REUSEADDR for TCP ONLY. It matches docker's publish bind so a TCP port
    # in TIME_WAIT (which docker could still bind) reads free here instead of
    # forcing a needless sticky-port rotation right after a stop. NOT for UDP:
    # UDP has no TIME_WAIT, and SO_REUSEADDR on a UDP socket lets it bind a port
    # another live socket already holds — which would make is_port_free report a
    # busy UDP port as free and hand the same port to two instances (the
    # WireGuard datapath the sticky/range design exists to protect).
    if protocol != 'udp':
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)


def get_free_ports(host='127.0.0.1', numbers=1, protocol='tcp'):
    # TCP and UDP are independent port namespaces, so probe with the matching
    # socket type. All sockets in a batch are held open until the end so the
    # same number is not handed out twice within one call.
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


def is_port_free(host, port, protocol='tcp'):
    """True if ``port`` can be bound on ``host`` for ``protocol`` right now.

    Targeted probe used to decide whether a sticky (previously-allocated) L4
    port can be reused. Probe the SAME interface the port will be published on
    (proxy mode binds 0.0.0.0) — a port held on another interface would
    otherwise read free. Best-effort: there is a TOCTOU window between this
    check and the container binding, but L4 ports come from a dedicated range
    (not the ephemeral range), so transient occupants are rare.
    """
    sock = socket.socket(socket.AF_INET, _sock_type(protocol))
    _maybe_reuseaddr(sock, protocol)
    try:
        sock.bind((host, int(port)))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def allocate_ports_in_range(host, numbers, range_start, range_end,
                            protocol='tcp', reserved=()):
    """Allocate ``numbers`` free ports in [range_start, range_end] on ``host``.

    Sockets are held open across the batch (then closed) so the same port is
    not handed out twice within one call. ``reserved`` ports (e.g. sticky ports
    being reused this batch) are skipped so a fresh allocation can't collide
    with one. Raises RuntimeError if the range can't satisfy the request.
    """
    sock_type = _sock_type(protocol)
    reserved = set(int(p) for p in reserved)
    socks = []
    ports = []
    try:
        for candidate in range(int(range_start), int(range_end) + 1):
            if len(ports) >= numbers:
                break
            if candidate in reserved:
                continue
            sock = socket.socket(socket.AF_INET, sock_type)
            _maybe_reuseaddr(sock, protocol)
            try:
                sock.bind((host, candidate))
            except OSError:
                sock.close()
                continue
            ports.append(candidate)
            socks.append(sock)
        if len(ports) < numbers:
            raise RuntimeError(
                f"L4 port range {range_start}-{range_end} exhausted: needed "
                f"{numbers}, found {len(ports)} free")
        return ports
    finally:
        for sock in socks:
            sock.close()