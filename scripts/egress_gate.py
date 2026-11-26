"""Single-rule egress gate: one TCP port in, one host:port out.

Runs on two docker networks: the internal one the agent containers live
on, and the routable bridge from which the LLM is reachable. Nothing
else is forwarded, so an agent under full prompt-injection control still
has exactly one destination. Configure with GATE_LISTEN (port) and
GATE_TARGET (host:port).

The gate rewrites the HTTP Host header of the first request on each
connection to the upstream address, because modern local servers
(Strata, llama.cpp) reject foreign Host headers as DNS rebinding. That
matches the one-request-per-connection clients the factory ships with
(urllib); a keep-alive client would see the rewrite only on its first
request and fail loudly, never silently.
"""

import os
import socket
import threading

LISTEN = int(os.environ.get("GATE_LISTEN", "8080"))
_host, _, _port = os.environ.get("GATE_TARGET", "127.0.0.1:8080").rpartition(":")
TARGET_HOST = _host or "127.0.0.1"
TARGET_PORT = int(_port)
HOST_HEADER = ("Host: %s:%d" % (TARGET_HOST, TARGET_PORT)).encode()


def _read_head(sock):
    buf = b""
    while b"\r\n\r\n" not in buf and b"\n\n" not in buf:
        data = sock.recv(4096)
        if not data:
            return (buf, b"") if buf else (None, b"")
        buf += data
    for sep in (b"\r\n\r\n", b"\n\n"):
        idx = buf.find(sep)
        if idx != -1:
            return buf[:idx + len(sep)], buf[idx + len(sep):]
    return buf, b""


def _rewrite_host(head):
    lines = head.split(b"\r\n")
    request_line, rest = lines[0], lines[1:]
    while rest and rest[-1] == b"":
        rest.pop()
    kept = [line for line in rest
            if not line.lower().startswith(b"host:")]
    return b"\r\n".join([request_line, HOST_HEADER] + kept + [b""]) + b"\r\n"


def _pipe(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for sock in (src, dst):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _serve(client):
    try:
        upstream = socket.create_connection((TARGET_HOST, TARGET_PORT),
                                            timeout=30)
    except OSError:
        client.close()
        return
    head, leftover = _read_head(client)
    if head is not None:
        try:
            upstream.sendall(_rewrite_host(head))
            if leftover:
                upstream.sendall(leftover)
        except OSError:
            client.close()
            upstream.close()
            return
    threads = [threading.Thread(target=_pipe, args=(client, upstream),
                                daemon=True),
               threading.Thread(target=_pipe, args=(upstream, client),
                                daemon=True)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for sock in (client, upstream):
        try:
            sock.close()
        except OSError:
            pass


def main():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", LISTEN))
    server.listen(16)
    print(f"egress gate: 0.0.0.0:{LISTEN} -> {TARGET_HOST}:{TARGET_PORT}",
          flush=True)
    while True:
        client, _ = server.accept()
        threading.Thread(target=_serve, args=(client,), daemon=True).start()


if __name__ == "__main__":
    main()
