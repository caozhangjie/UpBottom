"""Shared runtime constants for scheduled UpBottom jobs.

Runtime data is intentionally not split by dataset. The cloud layout is:

    /data/UpBottom/data/{1day,4h}
    /data/UpBottom/outputs
    /data/UpBottom/tmp
"""

import os
from pathlib import Path


RUNTIME_ROOT = Path(os.environ.get("UPBOTTOM_RUNTIME_ROOT") or "/data/UpBottom")
DATA_ROOT = RUNTIME_ROOT / "data"
OUTPUT_ROOT = RUNTIME_ROOT / "outputs"
TMP_ROOT = RUNTIME_ROOT / "tmp"
TMP_MINUTE_ROOT = TMP_ROOT / "1min"
PUSH_STATE_ROOT = OUTPUT_ROOT / "push_state"


try:
    from credentials import FEISHU_WEBHOOK_WATERLINE_SIGNAL as CREDENTIALS_FEISHU_WEBHOOK_WATERLINE_SIGNAL
except ImportError:
    CREDENTIALS_FEISHU_WEBHOOK_WATERLINE_SIGNAL = ""

try:
    from credentials import FEISHU_WEBHOOK_WATERLINE_TRADE as CREDENTIALS_FEISHU_WEBHOOK_WATERLINE_TRADE
except ImportError:
    CREDENTIALS_FEISHU_WEBHOOK_WATERLINE_TRADE = ""

try:
    from credentials import FEISHU_WEBHOOK_BOTTOM_HISTORY as CREDENTIALS_FEISHU_WEBHOOK_BOTTOM_HISTORY
except ImportError:
    CREDENTIALS_FEISHU_WEBHOOK_BOTTOM_HISTORY = ""

try:
    from credentials import FEISHU_WEBHOOK_BOTTOM_BUY as CREDENTIALS_FEISHU_WEBHOOK_BOTTOM_BUY
except ImportError:
    CREDENTIALS_FEISHU_WEBHOOK_BOTTOM_BUY = ""

try:
    from credentials import FEISHU_WEBHOOK_BOTTOM_SELL as CREDENTIALS_FEISHU_WEBHOOK_BOTTOM_SELL
except ImportError:
    CREDENTIALS_FEISHU_WEBHOOK_BOTTOM_SELL = ""


FEISHU_WEBHOOKS = {
    "waterline_signal": os.environ.get("FEISHU_WEBHOOK_WATERLINE_SIGNAL", "") or CREDENTIALS_FEISHU_WEBHOOK_WATERLINE_SIGNAL,
    "waterline_trade": os.environ.get("FEISHU_WEBHOOK_WATERLINE_TRADE", "") or CREDENTIALS_FEISHU_WEBHOOK_WATERLINE_TRADE,
    "bottom_history": os.environ.get("FEISHU_WEBHOOK_BOTTOM_HISTORY", "") or CREDENTIALS_FEISHU_WEBHOOK_BOTTOM_HISTORY,
    "bottom_buy": os.environ.get("FEISHU_WEBHOOK_BOTTOM_BUY", "") or CREDENTIALS_FEISHU_WEBHOOK_BOTTOM_BUY,
    "bottom_sell": os.environ.get("FEISHU_WEBHOOK_BOTTOM_SELL", "") or CREDENTIALS_FEISHU_WEBHOOK_BOTTOM_SELL,
}
