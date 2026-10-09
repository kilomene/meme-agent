"""Allow `python3 -m daemon.telegram` (supervisor entry point)."""
import sys
from daemon.telegram import main_entry

if __name__ == "__main__":
    sys.exit(main_entry(sys.argv[1:]))
