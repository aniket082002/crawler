"""Instagram integration for WebCrawler, using the official Instagram Platform
(Graph API) only.

PHASE 1 (this package as it stands): configuration for both authentication
modes (Instagram Login and Facebook Login), target validation, a transport-
agnostic API client foundation, error classification, secret redaction, the
capability model and the data normalizer.

Phase 1 contains no network code, no login/OAuth, no token exchange or
refresh, and no data extraction. The package is independent of the website
crawler: it imports nothing from Scrapy or the spider, and the crawler does
not import it. Standard library only.

REQUIREMENTS FOR LATER PHASES (from the Phase 1 security audit)
---------------------------------------------------------------
Transport (Phase 2): follow the TRANSPORT CONTRACT in client.py - HTTPS with
default certificate/hostname verification, approved hosts only, never follow
redirects, bounded streaming reads, timeouts, credentials only from
PreparedRequest.transport_headers() at send time.

Token exchange/refresh (Phase 2): the API client never places tokens in URLs
and rejects credential query parameters. Meta's token exchange/refresh
endpoints take credentials as query parameters; they must get their own
narrowly scoped function that is not usable for ordinary API requests, uses
the redactor for every URL it logs, and never logs the response body.

Logging (Phase 2+): package code must obtain loggers via
redaction.get_logger(). An Instagram entry point must call
redaction.protect_handlers() on the handlers it configures, so records from
any logger are redacted at output.

Docker and launcher (Phase 4) - required before an Instagram container run:
  * Pass credentials BY NAME only (config.docker_env_passthrough: "-e NAME"),
    never "-e NAME=value"; the launcher logs the full docker command. The app
    secret is never passed into the container.
  * Values passed with -e are visible to local Docker users via
    `docker inspect` and inside the container's environment. Run only on a
    machine whose Docker users are trusted; use short-lived tokens.
  * Keep any .env file OUTSIDE the project folder (or add a .dockerignore
    excluding .env before one is introduced). The Dockerfile copies only
    webcrawler/ and scrapy.cfg, so a root .env is not built into the image,
    but the documented test command mounts the whole project folder
    read-only, which would expose a project-root .env to the test container.
  * Keep the existing container limits: non-root user, --cap-drop ALL (two
    separate arguments), no-new-privileges, memory and CPU limits.
These are integration-point changes to existing files (launcher, README,
Docker build context) and need explicit approval when Phase 4 is reached.
"""

PHASE = 1

__all__ = ["PHASE"]
