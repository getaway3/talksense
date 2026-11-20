import os
import sys
from pathlib import Path

os.environ["TALKSENSE_NO_AUTOAPP"] = "1"
os.environ.setdefault("SERVICE_TOKEN", "test-service-token")
HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "common"))
