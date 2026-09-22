# -*- coding: utf-8 -*-
"""RefSync-QA: embodied multimodal (LeRobot v2.1) data-quality detection and safe governance operator."""
from .config import VERSION

__version__ = VERSION


def run(*args, **kwargs):
    from .pipeline import run as _run
    return _run(*args, **kwargs)


__all__ = ["run", "VERSION", "__version__"]
