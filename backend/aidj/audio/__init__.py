"""Backend-side audio utilities (peak extraction, etc.).

Heavy audio analysis still lives in plugins; this module is reserved for
small, on-the-fly utilities the frontend needs (e.g. waveform peaks for
the track-detail view) that don't justify spinning up a plugin subprocess.
"""
