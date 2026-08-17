"""Where the classifier artifact lives.

Resolved relative to this package rather than the working directory, because
the server runs from ``/home/daintree`` in the container while the artifact
ships inside site-packages.
"""

from __future__ import annotations

from pathlib import Path


def classifier_artifact_dir() -> Path:
    return Path(__file__).resolve().parent / "artifacts" / "classifier"
