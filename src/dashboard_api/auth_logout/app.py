import logging
import os

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_EXPIRED_COOKIE = "session=; HttpOnly; Secure; SameSite=Lax; Max-Age=0; Path=/"


def lambda_handler(event: dict, context: object) -> dict:
    host = os.environ.get("FRONTEND_DOMAIN", "")
    login_url = f"https://{host}/api/v1/auth/login" if host else "/api/v1/auth/login"
    return {
        "statusCode": 302,
        "headers": {
            "Set-Cookie": _EXPIRED_COOKIE,
            "Location": login_url,
        },
        "body": "",
    }
