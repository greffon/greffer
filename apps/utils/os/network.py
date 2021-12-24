import socket

def get_free_ports(host='127.0.0.1', numbers=1):
    socks = []
    ports = []
    for _ in range(numbers):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind((host, 0))
        ports.append(sock.getsockname()[1])
        socks.append(sock)
    for i in range(numbers):
        socks[i].close()
    return ports 