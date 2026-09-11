"""Fail-safe test configuration loaded before any application modules.

The test modules import the sourcing package in different orders. Environment
is therefore pinned here, before collection, so an early config import can
never select a developer or installed candidate database.
"""
from __future__ import annotations

import os
import tempfile


_TEST_ROOT = tempfile.mkdtemp(prefix="radixsol-tests-")
os.environ["ENFORMION_DEMO"] = "1"
os.environ["SOURCING_DB"] = os.path.join(_TEST_ROOT, "sourcing-test.db")
os.environ["DATABASE_BACKEND"] = "sqlite"
os.environ["DATABASE_URL"] = ""
os.environ["STORAGE_ENABLED"] = "0"
os.environ["PDL_API_KEY"] = ""
os.environ["PDL_ENABLED"] = "0"
os.environ["PDL_TRUST_PROVIDER_MATCH"] = "0"
# The PDL/Enformion suite must keep exercising that waterfall, and no test
# may reach the external Quick Sourcer service.
os.environ["CONTACT_LOOKUP_PROVIDER"] = "people_data_labs"
os.environ["QUICK_SOURCER_API_KEY"] = ""
os.environ["QUICK_SOURCER_ENABLED"] = "0"
os.environ["QUICK_SOURCER_TRUSTED_FOR_SYNC"] = "0"
os.environ["NEXUS_REFERENCE_ENV"] = ""
os.environ["NEXUS_REFERENCE_CONFIG"] = ""
os.environ["NEXUS_SYNC_ENABLED"] = "0"
os.environ["RESUME_OCR_ENABLED"] = "0"
os.environ["WATCHER_EMAIL_NOTIFICATIONS_ENABLED"] = "0"
os.environ["SENDGRID_API_KEY"] = ""
os.environ["WATCHER_NOTIFICATION_EMAILS"] = ""
