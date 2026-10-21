"""Runtime configuration read by the services."""
from __future__ import annotations

import os

PAYMENT_RETRY_LIMIT = int(os.getenv("PAYMENT_RETRY_LIMIT", "3"))
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "60"))
ORDER_TIMEOUT_SECONDS = int(os.getenv("ORDER_TIMEOUT_SECONDS", "5"))
FEATURE_CHECKOUT_V2_ENABLED = os.getenv("FEATURE_CHECKOUT_V2_ENABLED", "false") == "true"
