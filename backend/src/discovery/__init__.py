"""Discovery sources.

Each module exposes a `pull()` function returning a list[RawJob]. `RawJob`
is a plain dict; the per-source shape is normalized in persist.py.
"""
