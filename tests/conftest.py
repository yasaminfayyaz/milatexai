"""Shared test setup.

Tests must pass on a clean machine (CI) with no .env. leafbridge.asgi builds
the real production app at import time and requires WORKOS_AUTHKIT_DOMAIN, so
give it a harmless placeholder here. conftest runs before leafbridge.config
loads .env, and load_dotenv never overrides, so local runs also use the
placeholder instead of production settings. A value already exported in the
shell still wins (setdefault).
"""

import os

os.environ.setdefault("WORKOS_AUTHKIT_DOMAIN", "https://placeholder.authkit.invalid")
