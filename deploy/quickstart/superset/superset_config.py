from __future__ import annotations

import base64
import os
from pathlib import Path

SECRET_KEY = os.environ["HEINZEL_SUPERSET_SECRET_KEY"]
SQLALCHEMY_DATABASE_URI = "sqlite:////app/superset_home/superset.db"
WTF_CSRF_ENABLED = True
FAB_ADD_SECURITY_API = True
FEATURE_FLAGS = {"DASHBOARD_RBAC": True}

# The chrome ships a 1x1 beacon to a third-party analytics gateway on page load, which tells
# someone outside the deployment that this deployment exists and when a tenant opened a
# dashboard -- and names the engine, under its own domain, in a tenant's network tab. There is
# no setting for it in this version; it is compiled into the bundle. So the policy that permits
# it is withdrawn instead, and the browser refuses the request.
#
# Subtracted from the shipped policy rather than restated, so a later version that adds a
# legitimate image source keeps it.
_BEACON_HOSTS = frozenset({"https://apachesuperset.gateway.scarf.sh", "https://static.scarf.sh/"})


def _without_beacons(policy: dict[str, object]) -> dict[str, object]:
    sources = policy.get("img-src")
    if not isinstance(sources, list):
        return policy
    return {**policy, "img-src": [s for s in sources if s not in _BEACON_HOSTS]}


# The dashboard surface is part of the product a stakeholder is handed, not a second tool they
# were sent to, so it carries the product's name and mark rather than the engine's. The Apache
# License grants no rights in the Apache Software Foundation's marks (clause 6), so presenting
# someone else's logo to a tenant would be the thing needing permission, not removing it. What
# the licence does require -- the LICENSE and NOTICE shipped inside the image -- is untouched;
# nothing here modifies or redistributes the software, it configures it. A deployment should
# still carry a third-party notices page naming the components it is built on.
_BRANDING = Path(__file__).resolve().parent / "branding"


def _inline_svg(name: str) -> str:
    """An SVG as a data URI, because the mark has to reach a chrome that serves no static files.

    The supplied artwork is outlined vector paths with no linked images, scripts, stylesheets or
    live fonts, which is what makes this possible: it renders inside an `<img>`, where the page's
    own faces are not available and lettering set as text would fall back to whatever sans the
    viewer's machine has. `brand/README.md` is where these files come from and the only place
    they are changed; `apps/console` fails `check:brand` when this copy has drifted from it.
    """
    encoded = base64.b64encode((_BRANDING / name).read_bytes()).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


APP_NAME = "Heinzel"
FAVICONS = [{"href": _inline_svg("heinzel-symbol-color.svg")}]

# Superset 6 reads its chrome from theme tokens; `APP_ICON` alone no longer reaches the navbar.
THEME_DEFAULT = {
    "token": {
        "brandAppName": APP_NAME,
        "brandLogoAlt": APP_NAME,
        # The colour lockup, because this chrome runs the light algorithm below and its navbar
        # is a pale surface. Height is the only dimension the chrome takes: the artwork is
        # 353.62 by 112, so 45px tall is 142px wide -- just over the 140px the brand calls the
        # legibility floor for the full lockup, which is why it is not smaller.
        "brandLogoUrl": _inline_svg("heinzel-horizontal-color.svg"),
        "brandLogoHref": "/",
        "brandLogoHeight": "45px",
        "brandLogoMargin": "8px 0",
        # Heinzel blue, the console's own accent, so an action means the same thing on both
        # sides of the link. See the palette in `brand/README.md`.
        "colorPrimary": "#2155F5",
        "colorLink": "#2155F5",
        "colorSuccess": "#0f6b45",
        "colorWarning": "#7a4a09",
        "colorError": "#9f1d1d",
    },
    "algorithm": "default",
}


# Applied last, so it subtracts from whatever the installed version shipped. Imported here
# rather than restated because `superset.config` is already fully loaded by the time it
# executes this file -- it is the module doing the loading.
from superset import config as _shipped  # noqa: E402

TALISMAN_CONFIG = {
    **_shipped.TALISMAN_CONFIG,
    "content_security_policy": _without_beacons(
        dict(_shipped.TALISMAN_CONFIG["content_security_policy"])
    ),
}
