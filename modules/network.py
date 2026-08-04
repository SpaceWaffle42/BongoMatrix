"""Small network health helper retained from the old framework."""

import socket


def has_network(host="1.1.1.1", port=53, timeout=3):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
