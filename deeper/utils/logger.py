"""
Centralized logging configuration using loguru.
Creates separate log files for agent activity, research steps, and errors.
"""
import sys
import os
from loguru import logger

_configured = False


def setup_logging(logs_dir: str = "logs") -> None:
    """Configure loguru with multiple sinks."""
    global _configured
    if _configured:
        return

    os.makedirs(logs_dir, exist_ok=True)

    # Remove default sink
    logger.remove()

    # Console sink — INFO and above
    logger.add(
        sys.stdout,
        level="INFO",
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan> | "
            "<level>{message}</level>"
        ),
        colorize=True,
    )

    # General agent log
    logger.add(
        os.path.join(logs_dir, "agent.log"),
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} | {message}",
        rotation="10 MB",
        retention="7 days",
        compression="zip",
        encoding="utf-8",
    )

    # Research-specific log
    logger.add(
        os.path.join(logs_dir, "research.log"),
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}",
        rotation="10 MB",
        retention="7 days",
        compression="zip",
        encoding="utf-8",
        filter=lambda record: "research" in record["name"].lower()
        or record.get("extra", {}).get("research", False),
    )

    # Errors log
    logger.add(
        os.path.join(logs_dir, "errors.log"),
        level="ERROR",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} | {message}\n{exception}",
        rotation="5 MB",
        retention="30 days",
        compression="zip",
        encoding="utf-8",
        backtrace=True,
        diagnose=True,
    )

    _configured = True
    logger.info("Logging initialized — logs dir: {}", logs_dir)


def get_logger(name: str):
    """Return a logger bound with a module name."""
    return logger.bind(name=name)
