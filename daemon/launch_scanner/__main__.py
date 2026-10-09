"""Allow `python3 -m daemon.launch_scanner` (supervisor entry point)."""
import sys
from daemon.launch_scanner import main_entry

if __name__ == "__main__":
    sys.exit(main_entry(sys.argv[1:]))
