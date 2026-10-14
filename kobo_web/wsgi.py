import logging
import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "kobo_web.settings")
application = get_wsgi_application()

# Pick up background work that a restart/redeploy interrupted (see filler.tasks.resume_after_restart).
# Only here, so management commands like migrate never start browsers.
if os.environ.get("RESUME_ON_START", "1") == "1":
    try:
        from filler.tasks import resume_after_restart
        resume_after_restart()
    except Exception:
        logging.getLogger("filler").exception("could not resume unfinished work")
