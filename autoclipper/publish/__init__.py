"""Step 4: auto-publish finished clips to YouTube Shorts, Facebook Page Reels and Instagram Reels.

English clips go to the "en" accounts and Hindi clips to the "hi" accounts (settings.publish). Nothing is
sent while `publish.dry_run` is true (the default): a dry run only writes the request plan. Contract:
docs/CONTRACT_STEP4.md. Web code talks to this package only through `service.py`.
"""
from __future__ import annotations

import logging

PLATFORMS: tuple[str, ...] = ("youtube", "facebook", "instagram")
LANGS: tuple[str, ...] = ("en", "hi")
LANG_NAMES = {"en": "English", "hi": "Hindi"}
PLATFORM_NAMES = {"youtube": "YouTube", "facebook": "Facebook", "instagram": "Instagram"}

# httpx logs every request URL at INFO; keep it quiet inside the app (URLs may carry ids we do not log).
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
