import traceback
from pathlib import Path

from app.main_window import main

if __name__ == "__main__":
    try:
        main()
    except Exception:
        log = Path(__file__).resolve().parents[1] / "crash.log"
        log.write_text(traceback.format_exc(), encoding="utf-8")
        raise
