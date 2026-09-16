from __future__ import annotations

import os

SECRET_KEY = os.environ["PILLARMESH_SUPERSET_SECRET_KEY"]
SQLALCHEMY_DATABASE_URI = "sqlite:////app/superset_home/superset.db"
WTF_CSRF_ENABLED = True
FAB_ADD_SECURITY_API = True
FEATURE_FLAGS = {"DASHBOARD_RBAC": True}
