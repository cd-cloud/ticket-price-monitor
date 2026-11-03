from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent / "vendor" / "flyclaw"
sys.path.insert(0, str(ROOT))

from flyclaw import main


if __name__ == "__main__":
    main()
