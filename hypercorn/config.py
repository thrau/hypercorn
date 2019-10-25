import importlib
import importlib.util
import logging
import os
import socket
import ssl
import stat
import types
import warnings
from dataclasses import dataclass
from ssl import SSLContext, VerifyFlags, VerifyMode
from time import time
from typing import Any, AnyStr, Dict, List, Mapping, Optional, Tuple, Type, Union
from wsgiref.handlers import format_date_time

import toml

from .logging import Logger

BYTES = 1
OCTETS = 1
SECONDS = 1.0

FilePath = Union[AnyStr, os.PathLike]


@dataclass
class Sockets:
    secure_sockets: List[socket.socket]
    insecure_sockets: List[socket.socket]
    quic_sockets: List[socket.socket]


class Config:
    _bind = ["127.0.0.1:8000"]
    _insecure_bind: List[str] = []
    _quic_bind: List[str] = []
    _log: Optional[Logger] = None

    access_log_format = '%(h)s %(l)s %(l)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s"'
    accesslog: Union[logging.Logger, str, None] = None
    alpn_protocols = ["h2", "http/1.1"]
    alt_svc_headers: List[str] = []
    application_path: str
    backlog = 100
    ca_certs: Optional[str] = None
    certfile: Optional[str] = None
    ciphers: str = "ECDHE+AESGCM"
    debug = False
    dogstatsd_tags = ""
    errorlog: Union[logging.Logger, str, None] = "-"
    group: Optional[int] = None
    h11_max_incomplete_size = 16 * 1024 * BYTES
    h2_max_concurrent_streams = 100
    h2_max_header_list_size = 2 ** 16
    h2_max_inbound_frame_size = 2 ** 14 * OCTETS
    include_server_header = True
    keep_alive_timeout = 5 * SECONDS
    keyfile: Optional[str] = None
    logconfig: Optional[str] = None
    logconfig_dict: Optional[dict] = None
    logger_class = Logger
    loglevel: str = "info"
    max_app_queue_size: int = 10
    pid_path: Optional[str] = None
    root_path = ""
    shutdown_timeout = 60 * SECONDS
    ssl_handshake_timeout = 60 * SECONDS
    startup_timeout = 60 * SECONDS
    statsd_host: Optional[str] = None
    statsd_prefix = ""
    umask: Optional[int] = None
    use_reloader = False
    user: Optional[int] = None
    verify_flags: Optional[VerifyFlags] = None
    verify_mode: Optional[VerifyMode] = None
    websocket_max_message_size = 16 * 1024 * 1024 * BYTES
    worker_class = "asyncio"
    workers = 1

    def set_cert_reqs(self, value: int) -> None:
        warnings.warn("Please use verify_mode instead", Warning)
        self.verify_mode = VerifyMode(value)

    cert_reqs = property(None, set_cert_reqs)

    @property
    def log(self) -> Logger:
        if self._log is None:
            self._log = self.logger_class(self)
        return self._log

    @property
    def bind(self) -> List[str]:
        return self._bind

    @bind.setter
    def bind(self, value: Union[List[str], str]) -> None:
        if isinstance(value, str):
            self._bind = [value]
        else:
            self._bind = value

    @property
    def insecure_bind(self) -> List[str]:
        return self._insecure_bind

    @insecure_bind.setter
    def insecure_bind(self, value: Union[List[str], str]) -> None:
        if isinstance(value, str):
            self._insecure_bind = [value]
        else:
            self._insecure_bind = value

    @property
    def quic_bind(self) -> List[str]:
        return self._quic_bind

    @quic_bind.setter
    def quic_bind(self, value: Union[List[str], str]) -> None:
        if isinstance(value, str):
            self._quic_bind = [value]
        else:
            self._quic_bind = value

    def create_ssl_context(self) -> Optional[SSLContext]:
        if not self.ssl_enabled:
            return None

        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.set_ciphers(self.ciphers)
        cipher_opts = 0
        for attr in ["OP_NO_SSLv2", "OP_NO_SSLv3", "OP_NO_TLSv1", "OP_NO_TLSv1_1"]:
            if hasattr(ssl, attr):  # To be future proof
                cipher_opts |= getattr(ssl, attr)
        context.options |= cipher_opts  # RFC 7540 Section 9.2: MUST be TLS >=1.2
        context.options |= ssl.OP_NO_COMPRESSION  # RFC 7540 Section 9.2.1: MUST disable compression
        context.set_alpn_protocols(self.alpn_protocols)

        if self.certfile is not None and self.keyfile is not None:
            context.load_cert_chain(certfile=self.certfile, keyfile=self.keyfile)

        if self.ca_certs is not None:
            context.load_verify_locations(self.ca_certs)
        if self.verify_mode is not None:
            context.verify_mode = self.verify_mode
        if self.verify_flags is not None:
            context.verify_flags = self.verify_flags

        return context

    @property
    def ssl_enabled(self) -> bool:
        return self.certfile is not None and self.keyfile is not None

    def create_sockets(self) -> Sockets:
        if self.ssl_enabled:
            secure_sockets = self._create_sockets(self.bind)
            insecure_sockets = self._create_sockets(self.insecure_bind)
            quic_sockets = self._create_sockets(self.quic_bind, socket.SOCK_DGRAM)
        else:
            secure_sockets = []
            insecure_sockets = self._create_sockets(self.bind)
            quic_sockets = []
        return Sockets(secure_sockets, insecure_sockets, quic_sockets)

    def _create_sockets(
        self, binds: List[str], type_: int = socket.SOCK_STREAM
    ) -> List[socket.socket]:
        sockets: List[socket.socket] = []
        for bind in binds:
            binding: Any = None
            if bind.startswith("unix:"):
                sock = socket.socket(socket.AF_UNIX, type_)
                binding = bind[5:]
                try:
                    if stat.S_ISSOCK(os.stat(binding).st_mode):
                        os.remove(binding)
                except FileNotFoundError:
                    pass
            elif bind.startswith("fd://"):
                sock = socket.fromfd(int(bind[5:]), socket.AF_UNIX, type_)
            else:
                bind = bind.replace("[", "").replace("]", "")
                try:
                    value = bind.rsplit(":", 1)
                    host, port = value[0], int(value[1])
                except (ValueError, IndexError):
                    host, port = bind, 8000
                sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, type_)
                if self.workers > 1:
                    try:
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                    except AttributeError:
                        pass
                binding = (host, port)

            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

            if bind.startswith("unix:"):
                if self.umask is not None:
                    current_umask = os.umask(self.umask)
                sock.bind(binding)
                if self.user is not None and self.group is not None:
                    os.chown(binding, self.user, self.group)
                if self.umask is not None:
                    os.umask(current_umask)
            elif bind.startswith("fd://"):
                pass
            else:
                sock.bind(binding)

            sock.setblocking(False)
            try:
                sock.set_inheritable(True)
            except AttributeError:
                pass
            sockets.append(sock)
        return sockets

    def response_headers(self, protocol: str) -> List[Tuple[bytes, bytes]]:
        headers = [(b"date", format_date_time(time()).encode("ascii"))]
        if self.include_server_header:
            headers.append((b"server", f"hypercorn-{protocol}".encode("ascii")))

        for alt_svc_header in self.alt_svc_headers:
            headers.append((b"alt-svc", alt_svc_header.encode()))
        if len(self.alt_svc_headers) == 0:
            for bind in self._quic_bind:
                port = int(bind.split(":")[-1])
                headers.append((b"alt-svc", b'h3-23=":%d"; ma=3600' % port))

        return headers

    def set_statsd_logger_class(self, statsd_logger: Type[Logger]) -> None:
        if self.logger_class == Logger and self.statsd_host is not None:
            self.logger_class = statsd_logger

    @classmethod
    def from_mapping(
        cls: Type["Config"], mapping: Optional[Mapping[str, Any]] = None, **kwargs: Any
    ) -> "Config":
        """Create a configuration from a mapping.

        This allows either a mapping to be directly passed or as
        keyword arguments, for example,

        .. code-block:: python

            config = {'keep_alive_timeout': 10}
            Config.from_mapping(config)
            Config.from_mapping(keep_alive_timeout=10)

        Arguments:
            mapping: Optionally a mapping object.
            kwargs: Optionally a collection of keyword arguments to
                form a mapping.
        """
        mappings: Dict[str, Any] = {}
        if mapping is not None:
            mappings.update(mapping)
        mappings.update(kwargs)
        config = cls()
        for key, value in mappings.items():
            try:
                setattr(config, key, value)
            except AttributeError:
                pass

        return config

    @classmethod
    def from_pyfile(cls: Type["Config"], filename: FilePath) -> "Config":
        """Create a configuration from a Python file.

        .. code-block:: python

            Config.from_pyfile('hypercorn_config.py')

        Arguments:
            filename: The filename which gives the path to the file.
        """
        file_path = os.fspath(filename)
        spec = importlib.util.spec_from_file_location("module.name", file_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore
        return cls.from_object(module)

    @classmethod
    def from_toml(cls: Type["Config"], filename: FilePath) -> "Config":
        """Load the configuration values from a TOML formatted file.

        This allows configuration to be loaded as so

        .. code-block:: python

            Config.from_toml('config.toml')

        Arguments:
            filename: The filename which gives the path to the file.
        """
        file_path = os.fspath(filename)
        with open(file_path) as file_:
            data = toml.load(file_)
        return cls.from_mapping(data)

    @classmethod
    def from_object(cls: Type["Config"], instance: Union[object, str]) -> "Config":
        """Create a configuration from a Python object.

        This can be used to reference modules or objects within
        modules for example,

        .. code-block:: python

            Config.from_object('module')
            Config.from_object('module.instance')
            from module import instance
            Config.from_object(instance)

        are valid.

        Arguments:
            instance: Either a str referencing a python object or the
                object itself.

        """
        if isinstance(instance, str):
            try:
                path, config = instance.rsplit(".", 1)
            except ValueError:
                path = instance
                instance = importlib.import_module(instance)
            else:
                module = importlib.import_module(path)
                instance = getattr(module, config)

        mapping = {
            key: getattr(instance, key)
            for key in dir(instance)
            if not isinstance(getattr(instance, key), types.ModuleType)
        }
        return cls.from_mapping(mapping)
