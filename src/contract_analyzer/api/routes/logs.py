"""The live console, as server-sent events.

One process-wide stream: the same compact lines stderr prints, fanned out to
every tab that is watching. It does not close; a client that hangs up is
dropped from the fan-out and a new one gets the replay buffer, then the live
lines.
"""

from __future__ import annotations

from fastapi import APIRouter, status
from sse_starlette.sse import EventSourceResponse

from ..deps import LogsDep, Protected, SettingsDep

router = APIRouter(prefix="/logs", tags=["logs"], dependencies=[Protected])


@router.get(
    "/events",
    summary="Live console as server-sent events",
    description=(
        "One `log` event per line the process writes, formatted the way stderr is. "
        "The stream stays open for the life of the process. Subscribing late replays "
        "a short buffer and then follows."
    ),
    responses={status.HTTP_200_OK: {"content": {"text/event-stream": {}}}},
)
def events(logs: LogsDep, settings: SettingsDep) -> EventSourceResponse:
    def stream():
        for event in logs.subscribe():
            yield {"event": event.name, "data": event.json}

    return EventSourceResponse(stream(), ping=int(settings.api_keepalive_seconds))
