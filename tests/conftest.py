import os

# Unit tests import app.db at collection time. Provide an explicit, test-only
# credential so the production config can reject missing database passwords.
os.environ.setdefault("POSTGRES_PASSWORD", "rag-test-only")
