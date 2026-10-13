import socket

import pytest

from tests.conftest import NetworkAccessError

# 192.0.2.0/24 is TEST-NET-1 (RFC 5737): never routable, so a broken guard
# times out instead of reaching a real host.
UNROUTABLE = ("192.0.2.1", 80)


def test_socket_connect_is_blocked() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        with pytest.raises(NetworkAccessError):
            sock.connect(UNROUTABLE)


def test_socket_connect_ex_is_blocked() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        with pytest.raises(NetworkAccessError):
            sock.connect_ex(UNROUTABLE)


def test_create_connection_is_blocked() -> None:
    with pytest.raises(NetworkAccessError):
        socket.create_connection(UNROUTABLE, timeout=0.5)


def test_dns_lookup_is_blocked() -> None:
    with pytest.raises(NetworkAccessError):
        socket.getaddrinfo("example.com", 443)
