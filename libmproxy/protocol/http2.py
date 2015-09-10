from __future__ import (absolute_import, print_function, division)
from Queue import Queue
import select
import socket
import threading
from io import BytesIO

from hpack.hpack import Encoder, Decoder
import backports.socketpair

from libmproxy.models import HTTPRequest, HTTPResponse
from netlib.http import Headers
from netlib.http.http1 import HTTP1Protocol
from netlib.http.http2 import Frame, HeadersFrame, ContinuationFrame, DataFrame, HTTP2Protocol, \
    SettingsFrame, WindowUpdateFrame
from netlib.tcp import Reader
from ..exceptions import Http2Exception
from .base import Layer
from .http import _StreamingHttpLayer, HttpLayer

assert backports.socketpair


CLIENT_CONNECTION_PREFACE = "PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"


class Http2Connection(object):
    def __init__(self, connection):
        self.encoder = Encoder()
        self.decoder = Decoder()
        self.write_lock = threading.RLock()
        self.via = connection
        if self.via:
            self.preface()

    def send_headers(self, headers, stream_id, end_stream=False):
        with self.write_lock:
            proto = HTTP2Protocol(encoder=self.encoder)
            frames = proto._create_headers(headers, stream_id, end_stream)
            self.send_frame(*frames)

    def send_data(self, data, stream_id, end_stream=False):
        proto = HTTP2Protocol()
        frames = proto._create_body(data, stream_id, end_stream)
        for frame in frames:
            self.send_frame(frame)

    def send_frame(self, *frames):
        """
        Should only be called with multiple frames if they MUST be send in sequence.
        """
        # TODO: Should be frames, not chunks
        with self.write_lock:
            for frame in frames:
                print("send frame", self.__class__.__name__, frame.human_readable())
                self.via.wfile.write(frame.to_bytes())
                self.via.wfile.flush()

    def send(self, *args):
        raise RuntimeError()

    def read_headers(self, headers_frame):
        all_header_frames = self._read_all_header_frames(headers_frame)
        header_block_fragment = b"".join(frame.header_block_fragment for frame in all_header_frames)
        headers = Headers(
            [[str(k), str(v)] for k, v in self.decoder.decode(header_block_fragment)]
        )
        return all_header_frames, headers

    def _read_all_header_frames(self, headers_frame):
        frames = [headers_frame]
        while not frames[-1].flags & Frame.FLAG_END_HEADERS:
            frame = Frame.from_file(self.via.rfile)  # TODO: max_body_size

            if not isinstance(frame, ContinuationFrame) or frame.stream_id != frames[-1].stream_id:
                raise Http2Exception("Unexpected frame: %s" % repr(frame))

            frames.append(frame)
        return frames

    def __nonzero__(self):
        return bool(self.via)

    def __getattr__(self, item):
        return getattr(self.via, item)

    def preface(self):
        raise NotImplementedError()


class Http2ClientConnection(Http2Connection):
    def preface(self):
        # Check Client Preface
        expected_client_preface = CLIENT_CONNECTION_PREFACE
        actual_client_preface = self.via.rfile.read(len(CLIENT_CONNECTION_PREFACE))
        if expected_client_preface != actual_client_preface:
            raise Http2Exception("Invalid Client preface: %s" % actual_client_preface)

        # Send Settings Frame
        settings_frame = SettingsFrame(settings={
            SettingsFrame.SETTINGS.SETTINGS_MAX_CONCURRENT_STREAMS: 100,
            SettingsFrame.SETTINGS.SETTINGS_INITIAL_WINDOW_SIZE: 2**31 - 1  # yolo flow control (tm)
        })
        self.send_frame(settings_frame)

        # yolo flow control (tm)
        window_update_frame = WindowUpdateFrame(stream_id=0, window_size_increment=2**31 - 2**16)
        self.send_frame(window_update_frame)


class Http2ServerConnection(Http2Connection):
    def connect(self):
        self.via.connect()
        self.preface()

    def preface(self):
        self.via.wfile.write(CLIENT_CONNECTION_PREFACE)
        self.via.wfile.flush()

        # Send Settings Frame
        settings_frame = SettingsFrame(settings={
            SettingsFrame.SETTINGS.SETTINGS_ENABLE_PUSH: 0,
            SettingsFrame.SETTINGS.SETTINGS_MAX_CONCURRENT_STREAMS: 100,
            SettingsFrame.SETTINGS.SETTINGS_INITIAL_WINDOW_SIZE: 2**31 - 1  # yolo flow control (tm)
        })
        self.send_frame(settings_frame)
        # yolo flow control (tm)
        window_update_frame = WindowUpdateFrame(stream_id=0, window_size_increment=2**31 - 2**16)
        self.send_frame(window_update_frame)


class Http2Layer(Layer):

    def __init__(self, ctx, mode):
        super(Http2Layer, self).__init__(ctx)
        if mode != "transparent":
            raise NotImplementedError("HTTP2 supports transparent mode only")

        self.client_conn = Http2ClientConnection(self.client_conn)
        self.server_conn = Http2ServerConnection(self.server_conn)

        self.active_conns = [self.client_conn.connection]
        if self.server_conn:
            self.active_conns.append(self.server_conn.connection)

        self.streams = {}

    def connect(self):
        if not self.server_conn:
            self.server_conn.connect()
            self.active_conns.append(self.server_conn.connection)

    def __call__(self):
        # TODO: Do we want to connect to the server default?
        # - yes: We may just connect and receive interesting PUSH_PROMISES
        # - no: We want server replay without connecting upstream.
        # self.connect()

        client = self.client_conn

        while True:
            r, _, _ = select.select(self.active_conns, [], [], 10)
            for conn in r:
                if conn == client.connection:
                    source = self.client_conn
                else:
                    source = self.server_conn

                frame = Frame.from_file(source.rfile)  # TODO: max_body_size
                print("receive frame", source.__class__.__name__, frame.human_readable())

                is_new_stream = (
                    isinstance(frame, HeadersFrame) and
                    source == client and
                    frame.stream_id not in self.streams
                )
                is_server_headers = (
                    isinstance(frame, HeadersFrame) and
                    source != client and
                    frame.stream_id in self.streams
                )
                is_data_frame = (
                    isinstance(frame, DataFrame) and
                    frame.stream_id in self.streams
                )
                is_settings_frame = (
                    isinstance(frame, SettingsFrame) and
                    frame.stream_id == 0
                )
                is_window_update_frame = (
                    isinstance(frame, WindowUpdateFrame)
                )
                if is_new_stream:
                    self._create_new_stream(frame)
                elif is_server_headers:
                    self._process_server_headers(frame)
                elif is_data_frame:
                    self._process_data_frame(frame, source)
                elif is_settings_frame:
                    self._process_settings_frame(frame, source)
                elif is_window_update_frame:
                    self._process_window_update_frame(frame)
                else:
                    raise Http2Exception("Unexpected Frame: %s" % repr(frame))

    def _process_window_update_frame(self, window_update_frame):
        pass  # yolo flow control (tm)

    def _process_settings_frame(self, settings_frame, source):
        if settings_frame.flags & Frame.FLAG_ACK:
            pass
        else:
            # yolo settings processing (tm) - fixme maybe
            settings_ack_frame = SettingsFrame(flags=Frame.FLAG_ACK)
            source.send_frame(settings_ack_frame)

    def _process_data_frame(self, data_frame, source):
        stream = self.streams[data_frame.stream_id]
        if source == self.client_conn.connection:
            target = stream.into_client_conn
        else:
            target = stream.into_server_conn

        target.sendall(data_frame.payload)

        if data_frame.flags & Frame.FLAG_END_STREAM:
            target.shutdown(socket.SHUT_WR)

    def _create_new_stream(self, headers_frame):
        header_frames, headers = self.client_conn.read_headers(headers_frame)
        stream = Stream(self, headers_frame.stream_id)
        self.streams[headers_frame.stream_id] = stream
        stream.start()

        stream.client_headers.put(headers)
        if header_frames[-1].flags & Frame.FLAG_END_STREAM:
            stream.into_client_conn.shutdown(socket.SHUT_WR)

    def _process_server_headers(self, headers_frame):
        header_frames, headers = self.server_conn.read_headers(headers_frame)
        stream = self.streams[headers_frame.stream_id]

        stream.server_headers.put(headers)
        if header_frames[-1].flags & Frame.FLAG_END_STREAM:
            stream.into_server_conn.shutdown(socket.SHUT_WR)


class FakeWFile(object):

    # TODO: Implement .close().

    def __init__(self, send_func):
        self.buf = BytesIO()
        self.send_func = send_func

    def write(self, b):
        self.buf.write(b)

    def flush(self):
        self.send_func(self.buf.getvalue())
        self.buf = BytesIO()


class StreamConnection(object):
    def __init__(self, original_connection, connection, send_func):
        self.original_connection = original_connection
        self.rfile = Reader(connection.makefile('rb', -1))
        self.wfile = FakeWFile(send_func)

    @property
    def address(self):
        return self.original_connection.address

    @property
    def tls_established(self):
        return self.original_connection.tls_established

    def __nonzero__(self):
        return bool(self.original_connection)


class Stream(_StreamingHttpLayer, threading.Thread):

    def __init__(self, ctx, stream_id):
        super(Stream, self).__init__(ctx)

        self.stream_id = stream_id

        a, b = socket.socketpair()
        self.client_conn = StreamConnection(self.ctx.client_conn, a, self._write_data_client)
        self.into_client_conn = b
        self.server_conn = StreamConnection(self.ctx.server_conn, b, self._write_data_server)
        self.into_server_conn = a

        self.client_headers = Queue()
        self.server_headers = Queue()

    def read_request(self):
        headers = self.client_headers.get()

        # All HTTP/2 requests MUST include exactly one valid value for the :method, :scheme, and
        # :path pseudo-header fields, unless it is a CONNECT request
        try:
            # TODO: Possibly .pop()?
            method = headers[':method']
            if method == "CONNECT":
                raise NotImplementedError("HTTP2 CONNECT not supported")
            else:
                host = None
                port = None
            scheme = headers[':scheme']
            path = headers[':path']
        except KeyError:
            raise Http2Exception("Malformed HTTP2 request")

        body = HTTP1Protocol(rfile=self.client_conn.rfile).read_http_body(
            headers,
            self.config.body_size_limit,
            method,
            None,
            True
        )

        return HTTPRequest(
            "relative",
            method,
            scheme,
            host,
            port,
            path,
            (2, 0),
            headers,
            body,
        )

    def send_request(self, request):
        # TODO: The end_stream stuff is too simple for a CONNECT request.
        self.ctx.server_conn.send_headers(
            request.headers,
            self.stream_id,
            end_stream=not request.body
        )
        if request.body:
            self.ctx.server_conn.send_data(request.body, self.stream_id, end_stream=True)

    def check_close_connection(self, flow):
        return True  # always close the stream

    def read_response_headers(self):

        # TODO: The first headers received from the server might be informational headers
        headers = self.server_headers.get()

        # All HTTP/2 responses MUST include exactly one valid value for the :status
        try:
            # TODO: Possibly .pop()?
            status = int(headers[':status'])
        except KeyError:
            raise Http2Exception("Malformed HTTP2 response")

        # TODO: Timestamps
        return HTTPResponse(
            (2, 0),
            status,
            "",
            headers,
            None
        )

    def read_response_body(self, headers, request_method, response_code, max_chunk_size=None):
        return HTTP1Protocol(rfile=self.server_conn.rfile).read_http_body(
            headers,
            self.config.body_size_limit,
            request_method,
            response_code,
            False
        )

    def send_response_headers(self, response):
        self.ctx.client_conn.send_headers(
            response.headers,
            self.stream_id,
            end_stream=not response.body
        )

    def send_response_body(self, response, chunks):
        if response.body:
            self.ctx.client_conn.send_data(response.body, self.stream_id, end_stream=True)

    def _write_data_client(self, data):
        self.ctx.client_conn.send_data(data, self.stream_id, end_stream=False)

    def _write_data_server(self, data):
        self.ctx.server_conn.send_data(data, self.stream_id, end_stream=False)

    def run(self):
        layer = HttpLayer(self, "transparent")
        layer()
