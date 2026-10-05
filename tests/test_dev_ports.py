"""Real loopback port probing: distinguish an active listener from TIME_WAIT."""

import errno
import socket

import pytest

from scripts.dev import assert_port_free


@pytest.mark.parametrize("host", ["127.0.0.1", "0.0.0.0"])
def test_probe_rejects_a_real_listener_even_with_address_reuse(host):
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        with pytest.raises(SystemExit, match=f"端口 {port} 已被占用"):
            assert_port_free(port)
        with listener.accept()[0] as probe_peer:
            probe_peer.settimeout(2)
            assert probe_peer.recv(1) == b""
        # The failed probe must not take ownership of or disrupt the existing server.
        with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
            with listener.accept()[0] as peer:
                peer.settimeout(2)
                client.sendall(b"still-listening")
                assert peer.recv(32) == b"still-listening"


def test_probe_and_listener_restart_after_a_closed_connection_in_time_wait():
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
            with listener.accept()[0] as peer:
                peer.settimeout(2)
                # Close from the server side first so its listening port acquires
                # a real TIME_WAIT connection after the FIN exchange completes.
                peer.shutdown(socket.SHUT_WR)
                assert client.recv(1) == b""
                client.shutdown(socket.SHUT_WR)
                assert peer.recv(1) == b""

    with socket.socket() as old_style_probe:
        with pytest.raises(OSError) as occupied:
            old_style_probe.bind(("127.0.0.1", port))
        assert occupied.value.errno == errno.EADDRINUSE

    assert_port_free(port)
    with socket.socket() as restarted:
        restarted.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        restarted.bind(("127.0.0.1", port))
        restarted.listen(1)
        with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
            with restarted.accept()[0] as peer:
                peer.settimeout(2)
                client.sendall(b"restarted")
                assert peer.recv(32) == b"restarted"
