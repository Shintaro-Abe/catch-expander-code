import json
import logging

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_EXPIRED_COOKIE = "session=; HttpOnly; Secure; SameSite=Lax; Max-Age=0; Path=/"


def lambda_handler(event: dict, context: object) -> dict:
    return {
        "statusCode": 200,
        "headers": {"Set-Cookie": _EXPIRED_COOKIE},
        "body": json.dumps({"message": "logged out"}),
    }
