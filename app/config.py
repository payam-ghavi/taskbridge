import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("TASKBRIDGE_DATA", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "state.db"
PORT = int(os.environ.get("PORT", "3737"))
