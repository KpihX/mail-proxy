"""
System logger for mail-proxy — logs to stderr for systemd/journald capture.

No file management: systemd handles log rotation and retention. There is no
`mail-proxy.log`, no rotating handler and no log-level env var — exactly like
`tg-proxy` and `tick-proxy`. stdout stays pure JSON so `mail-proxy do … | jq`
never breaks.
"""

import logging
import sys

logger = logging.getLogger("mail_proxy")


def setup_logging(level: str = "WARNING") -> logging.Logger:
    """Configure the package logger to write on stderr only.

    In a terminal → visible on stderr (never mixed with the stdout JSON).
    In a systemd service → captured by journalctl.

    Args:
        level (str): Logging level name — DEBUG, INFO, WARNING, ERROR.
            Defaults to WARNING so normal runs stay silent.

    Returns:
        logging.Logger: The configured `mail_proxy` logger (idempotent —
            calling it twice does not add a second handler).

    Examples:
        >>> setup_logging().level == 30          # WARNING
        True
        >>> setup_logging("DEBUG").level == 10   # DEBUG
        True
    """
    logger.setLevel(getattr(logging, level.upper(), logging.WARNING))

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        )
        logger.addHandler(handler)
    return logger
