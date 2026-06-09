import socket

def get_free_ports(host='127.0.0.1', numbers=1, protocol='tcp'):
    # TCP and UDP are independent port namespaces, so probe with the matching
    # socket type. All sockets in a batch are held open until the end so the
    # same number is not handed out twice within one call.
    sock_type = socket.SOCK_DGRAM if protocol == 'udp' else socket.SOCK_STREAM
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