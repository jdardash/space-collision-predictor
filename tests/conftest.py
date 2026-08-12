"""Put the repository root on sys.path for the whole test session.

`scripts/` is not part of the installed package (only `src/sda` is), so a test
that exercises a build script cannot import it without the repo root on the
path. pytest imports conftest before collecting any test module, which is what
lets those tests keep their imports at the top of the file.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
