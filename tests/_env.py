"""Point the app at a throwaway data dir before any app module is imported."""
import os
import tempfile

DATA_DIR = tempfile.mkdtemp(prefix="reclaimarr-test-")
os.environ["DATA_DIR"] = DATA_DIR
os.environ.setdefault("MEDIA_ROOTS", "/media2")
