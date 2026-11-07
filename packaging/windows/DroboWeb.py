"""Entry point for the Windows tray build (DroboWeb.exe)."""
import multiprocessing
import sys

from droboweb.tray import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    demo = ""
    if "--demo" in sys.argv:
        i = sys.argv.index("--demo")
        demo = sys.argv[i + 1] if i + 1 < len(sys.argv) and not sys.argv[i + 1].startswith("-") else "failing"
    main(demo=demo)
