"""micro.blog publishing endpoint.

Slice 3 of Wave 2a per docs/ux-compose-and-categories-design.md §7.2.

POST /api/microblog/publish accepts a `MicroblogPublishRequest`, invokes the
`MicroblogPublisher` (which calls Micropub and translates HTTP errors to
the three protocol exceptions), persists the audit row to `syndications`,
and returns the canonical post URL.

The Publisher itself lives in `lestash-microblog`. This route is the
"driver" half of the hexagonal architecture from the design doc — it knows
about HTTP and DB persistence; the adapter knows about Micropub; the
protocol contract sits between them.
"""

import json
import logging
from dataclasses import asdict

from fastapi import APIRouter, HTTPException
from lestash.plugins import (
    AlreadyPublished,
    ComposeRequest,
    PublishFailed,
    PublishRejected,
)
from lestash_microblog.client import create_client
from lestash_microblog.publisher import MicroblogPublisher

from lestash_server.deps import get_db
from lestash_server.models import MicroblogPublishRequest, MicroblogPublishResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/microblog", tags=["microblog"])


@router.post(
    "/publish",
    response_model=MicroblogPublishResponse,
    status_code=201,
)
async def publish(body: MicroblogPublishRequest) -> MicroblogPublishResponse:
    """Publish a composed item to micro.blog via Micropub.

    Status code mapping:
    - 201 Created → the post was created (success path).
    - 409 Conflict → already published; pass `if_not_already_published=true`
      to override.
    - 422 Unprocessable Entity → Micropub rejected the post (4xx). The
      provider's `error_description` is in `detail`.
    - 502 Bad Gateway → Micropub failed (5xx, network, missing Location).
      Caller may retry.
    - 401 Unauthorized → no micro.blog token configured locally.
    """
    compose = ComposeRequest(
        item_id=body.item_id,
        title=body.title,
        body=body.body,
        image_url=body.image_url,
        categories=tuple(body.categories),
        visibility=body.visibility,
    )

    try:
        client = create_client()
    except ValueError as e:
        raise HTTPException(
            status_code=401,
            detail=f"micro.blog not authenticated: {e}",
        ) from e

    publisher = MicroblogPublisher(client=client, get_db=get_db)

    try:
        result = await publisher.publish(
            compose,
            if_not_already_published=body.if_not_already_published,
        )
    except AlreadyPublished as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except PublishRejected as e:
        raise HTTPException(status_code=422, detail=e.message) from e
    except PublishFailed as e:
        raise HTTPException(status_code=502, detail=e.message) from e
    finally:
        client.close()

    # Audit row — request body and response body serialised verbatim. Per the
    # type-precision memory: raw_response is a Mapping, so we dict()-copy
    # before json.dumps to be defensive.
    with get_db() as conn:
        conn.execute(
            """INSERT INTO syndications
               (item_id, target, target_url, request_body, response_body)
               VALUES (?, ?, ?, ?, ?)""",
            (
                compose.item_id,
                result.target,
                result.url,
                json.dumps(asdict(compose)),
                json.dumps(dict(result.raw_response)),
            ),
        )
        conn.commit()

    logger.info(
        "Published item %d to %s: %s",
        compose.item_id,
        result.target,
        result.url,
    )
    return MicroblogPublishResponse(url=result.url)
