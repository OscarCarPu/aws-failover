import sys
from pathlib import Path

# The Lambda loads watchdog/ as the code root, so its modules import each other flat.
sys.path.insert(0, str(Path(__file__).parent.parent / "watchdog"))
