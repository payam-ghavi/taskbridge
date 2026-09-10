import logging

from waitress import serve

from .config import DB_PATH, PORT
from .loop import SyncLoop
from .store import Store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("taskbridge")


def main():
    # touch the DB / run migrations
    Store(DB_PATH).close()

    loop = SyncLoop(DB_PATH)
    loop.start()

    from . import web
    web.SYNC_LOOP = loop

    log.info("TaskBridge listening on :%d  (data: %s)", PORT, DB_PATH)
    serve(web.app, host="0.0.0.0", port=PORT, threads=8, ident="TaskBridge")


if __name__ == "__main__":
    main()
