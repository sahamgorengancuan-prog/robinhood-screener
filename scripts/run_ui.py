#!/usr/bin/env python3
"""Launch the Gradio control panel.

    python scripts/run_ui.py

Binds to 127.0.0.1:7860 by default. Override with GRADIO_HOST / GRADIO_PORT.

The UI has **no authentication** and can engage or release the kill switch, so
do not bind it to a public interface without putting an authenticating reverse
proxy in front of it.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ui.gradio_app import main  # noqa: E402

if __name__ == "__main__":
    main()
