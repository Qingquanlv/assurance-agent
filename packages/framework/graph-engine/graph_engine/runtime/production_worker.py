from graph_engine.attempts.production_worker import *  # noqa: F403
from graph_engine.attempts.host_protocol import TaskHostProtocolError
from graph_engine.attempts import production_worker as _impl
from graph_engine.attempts.production_worker import main
import sys

sys.modules[__name__] = _impl

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except TaskHostProtocolError as error:
        sys.stderr.write(str(error))
        raise SystemExit(1) from error
