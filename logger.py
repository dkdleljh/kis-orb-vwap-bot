import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo


def setup_logger(log_dir: str, tz: str) -> logging.Logger:
    """Project logger.

    By default logs to both file and stdout.

    For cron/automation scripts where stdout is treated as a user notification,
    set env `NO_STDOUT_LOG=1` to suppress stdout logging.
    """

    os.makedirs(log_dir, exist_ok=True)
    now = datetime.now(ZoneInfo(tz))
    log_path = os.path.join(log_dir, now.strftime("%Y-%m-%d") + ".log")

    logger = logging.getLogger("kis_bot")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    if os.environ.get("NO_STDOUT_LOG", "0") != "1":
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        logger.addHandler(sh)

    return logger
