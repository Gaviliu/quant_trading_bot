import logging
from logging.handlers import RotatingFileHandler
import os

def setup_logger(name: str, logfile: str, level=logging.INFO):
    os.makedirs("logs", exist_ok=True)

    formatter = logging.Formatter(
        "[%(asctime)s] %(levelname)-7s %(message)s",
        "%Y-%m-%d %H:%M:%S"
    )

    handler = RotatingFileHandler(
        f"logs/{logfile}", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(formatter)

    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.addHandler(handler)
    logger.propagate = False
    return logger
