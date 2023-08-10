# Standard
import signal

# First Party
import alog

# Local
from caikit.config.config import get_config
from caikit.runtime.grpc_server import RuntimeGRPCServer
from caikit.runtime.http_server import RuntimeHTTPServer

log = alog.use_channel("RUNTIME-MAIN")


def main():
    _grpc_server = None
    _http_server = None

    def interrupt(signal_, _stack_frame):
        log.info(
            "<RUN87630120I>",
            "Caikit Runtime received interrupt signal %s, shutting down",
            signal_,
        )
        if _grpc_server:
            _grpc_server.stop()
        if _http_server:
            _http_server.stop()

    # NOTE: signal function can only be called from main thread of the main
    # interpreter. If this function is called from a thread (like in tests)
    # then signal handler cannot be used. Thus, we will only have real
    # termination_handler when this is called from the __main__.

    signal.signal(signal.SIGINT, interrupt)
    signal.signal(signal.SIGTERM, interrupt)

    #####################
    # Start the servers
    #####################

    # Start serving grpc server
    if get_config().runtime.grpc.enabled:
        log.debug("Starting up caikit.runtime.grpc_server")

        _grpc_server = RuntimeGRPCServer()
        _grpc_server.start(blocking=not get_config().runtime.http.enabled)

    # Start serving http server
    if get_config().runtime.http.enabled:
        log.debug("Starting up caikit.runtime.http_server")

        _http_server = RuntimeHTTPServer()
        _http_server.start(blocking=True)  # make http always blocking


if __name__ == "__main__":
    main()
