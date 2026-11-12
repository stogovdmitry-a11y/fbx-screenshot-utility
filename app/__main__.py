import traceback
from app.paths import install_root

from app.main_window import main

if __name__ == "__main__":
    try:
        main()
    except Exception:
        log = install_root() / "crash.log"
        log.write_text(traceback.format_exc(), encoding="utf-8")
        raise
