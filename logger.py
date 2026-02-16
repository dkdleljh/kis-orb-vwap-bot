import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo


def setup_logger(log_dir: str, tz: str) -> logging.Logger:
    os.makedirs(log_dir, exist_ok=True)
    now = datetime.now(ZoneInfo(tz))
    log_path = os.path.join(log_dir, now.strftime("%Y-%m-%d") + ".log")

    logger = logging.getLogger("kis_bot")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger
