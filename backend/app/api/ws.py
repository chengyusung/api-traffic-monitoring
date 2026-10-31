from fastapi import APIRouter, WebSocket
from fastapi.security.utils import get_authorization_scheme_param

from app.api.deps import CurrentPipeline, Session, authenticate
from app.core.errors import UnauthorizedError, database_unavailable
from app.realtime.manager import CloseCode

router = APIRouter(tags=["realtime"])


@router.websocket("/ws/traffic")
async def traffic(websocket: WebSocket, session: Session, pipeline: CurrentPipeline) -> None:
    """Live simulator events (ADR-003). Pushed before commit: they are not persisted yet."""
    scheme, token = get_authorization_scheme_param(websocket.headers.get("Authorization"))
    rejection: CloseCode | None = None
    try:
        if pipeline.manager.full:
            # Before the user reload: a socket refused at the cap costs no database round trip.
            rejection = CloseCode.TRY_AGAIN_LATER
        elif scheme.lower() != "bearer" or not token:
            rejection = CloseCode.UNAUTHORIZED
        else:
            _, claims = await authenticate(token, session)
    except UnauthorizedError:
        rejection = CloseCode.UNAUTHORIZED
    except Exception as exc:
        if not database_unavailable(exc):
            raise
        rejection = CloseCode.TRY_AGAIN_LATER
    finally:
        # The socket can stay open for hours; it must not pin a pooled connection.
        await session.close()

    # Nothing is registered or sent before the check above. The socket is accepted even on
    # rejection because a close code only reaches the client after accept(); a close before
    # it turns into a bare HTTP 403.
    await websocket.accept()
    if rejection is not None:
        await websocket.close(rejection)
        return
    await pipeline.manager.serve(websocket, claims.expires_at, pipeline.status_message())
