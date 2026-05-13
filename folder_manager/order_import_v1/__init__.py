"""Order import package."""


def main(*args, **kwargs):
    # Lazy import avoids importing module side effects on package import.
    from .main import main as _main

    return _main(*args, **kwargs)


__all__ = ["main"]

