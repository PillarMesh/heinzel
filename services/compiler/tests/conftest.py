import os
import sys
from pathlib import Path

if os.getenv("MUTANT_UNDER_TEST"):
    sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
