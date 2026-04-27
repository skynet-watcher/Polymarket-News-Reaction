import os
import sys
from pathlib import Path

# Avoid RSS / LLM / heavy lag jobs during pytest (settings load after this file).
os.environ.setdefault("BACKGROUND_POLL_NEWS_INTERVAL_SECONDS", "0")
os.environ.setdefault("BACKGROUND_PROCESS_CANDIDATES_INTERVAL_SECONDS", "0")
os.environ.setdefault("BACKGROUND_LAG_PIPELINE_INTERVAL_SECONDS", "0")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

