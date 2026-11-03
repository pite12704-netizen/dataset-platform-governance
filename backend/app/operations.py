"""依赖探测与 Worker 心跳。失败时返回未就绪，不把排队服务当作正常处理。"""
import logging
import socket
import threading
import hashlib

from sqlalchemy.engine import make_url


def presence_key(settings, role):
    database = make_url(settings.database_url).database or "unknown"
    identity=hashlib.sha256(settings.secret_key.encode()).hexdigest()[:24]
    return f"dp:{role}:{database}:{identity}:"


def start_presence(settings, role):
    from redis import Redis
    client = Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
    stopped = threading.Event()
    key = presence_key(settings, role) + socket.gethostname()
    def pulse():
        while not stopped.is_set():
            try:
                client.set(key, "alive", ex=45)
            except Exception:
                logging.getLogger(__name__).warning("%s heartbeat unavailable", role)
            stopped.wait(10)
    threading.Thread(target=pulse, daemon=True, name="platform-heartbeat-" + role).start()
    return stopped


def scan_available(settings):
    with socket.create_connection((settings.clamav_host, settings.clamav_port), timeout=2) as connection:
        connection.sendall(b"zPING\0")
        if not connection.recv(64).startswith(b"PONG"):
            raise RuntimeError("Scan service not ready")
