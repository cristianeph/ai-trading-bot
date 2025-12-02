# common/monitoring.py
import logging
import os
import sentry_sdk
from sentry_sdk.integrations.logging import LoggingIntegration

from common.config import settings

def init_sentry() -> None:
    dsn = os.getenv("SENTRY_DSN")
    if not dsn:
        return  # do nothing if not configured

    # Logs integration: envía a Sentry todo lo que sea ERROR+
    sentry_logging = LoggingIntegration(
        level=logging.INFO,         # captures all logs
        event_level=logging.ERROR,   # only generates events whenever we want (see below)
    )

    sentry_sdk.init(
        dsn=dsn,
        integrations=[sentry_logging],
        environment=os.getenv("SENTRY_ENV", settings.TRADING_MODE),
        traces_sample_rate=0.0,    # 0 si no quieres APM/traces
    )