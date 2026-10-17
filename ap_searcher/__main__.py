"""Entry point: python -m ap_searcher"""

from __future__ import annotations

import argparse
import logging
import threading
import webbrowser
from pathlib import Path

from . import config as config_mod
from .jobs import JobManager
from .server import make_server


def main() -> None:
    parser = argparse.ArgumentParser(prog="ap-searcher", description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    config_path = Path(args.config)
    if not config_path.exists():
        example = config_path.with_name("config.example.yaml")
        hint = f" - copy {example} to {config_path} and edit it" if example.exists() else ""
        parser.error(f"no config at {config_path}{hint}")
    cfg = config_mod.load(config_path)
    if args.host:
        cfg.server.host = args.host
    if args.port:
        cfg.server.port = args.port

    httpd = make_server(cfg, JobManager(cfg))
    url = f"http://{cfg.server.host}:{cfg.server.port}/"
    logging.getLogger(__name__).info("ap-searcher on %s (office: %s)", url, cfg.office.name)
    if cfg.server.open_browser and not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("shutting down")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
