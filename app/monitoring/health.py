"""Health evaluation.

Health and readiness are deliberately different questions:

* **Health** -- "is this process functioning?" Used by the Docker healthcheck.
  A kill-switched, deliberately-not-trading bot is *healthy*: it is doing
  exactly what it was told. Marking it unhealthy would train operators to
  ignore the signal, and could trigger restart loops on a system whose safe
  state is "not trading".

* **Readiness** -- "may this process trade?" That is the transmit gate's
  question and it is reported separately in /status.

Only a genuine malfunction -- ERROR state or an unusable database -- is
unhealthy.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.enums import ApplicationState


@dataclass(frozen=True, slots=True)
class HealthResult:
    healthy: bool
    checks: dict[str, bool]
    detail: dict[str, object]

    @property
    def http_status(self) -> int:
        return 200 if self.healthy else 503

    def as_dict(self) -> dict[str, object]:
        return {
            "status": "healthy" if self.healthy else "unhealthy",
            "checks": self.checks,
            **self.detail,
        }


def evaluate_health(
    *,
    app_state: ApplicationState,
    database_ok: bool,
    shutting_down: bool,
) -> HealthResult:
    """Compute process health.

    ``shutting_down`` reports unhealthy so an orchestrator stops routing to a
    process that is on its way out.
    """
    checks = {
        "process_running": True,
        "database_reachable": database_ok,
        "application_not_errored": app_state is not ApplicationState.ERROR,
        "not_shutting_down": not shutting_down,
    }
    return HealthResult(
        healthy=all(checks.values()),
        checks=checks,
        detail={"application_state": app_state.value},
    )


__all__ = ["HealthResult", "evaluate_health"]
