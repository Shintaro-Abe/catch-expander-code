import hashlib
import hmac
import time


def verify_slack_signature(signing_secret: str, timestamp: str, body: str, signature: str) -> bool:
    """Slack署名検証（HMAC-SHA256）

    Args:
        signing_secret: Slack AppのSigning Secret
        timestamp: X-Slack-Request-Timestampヘッダーの値
        body: リクエストボディ（raw string）
        signature: X-Slack-Signatureヘッダーの値

    Returns:
        署名が有効な場合True
    """
    if abs(time.time() - int(timestamp)) > 60 * 5:
        return False

    sig_basestring = f"v0:{timestamp}:{body}"
    my_signature = (
        "v0="
        + hmac.new(
            signing_secret.encode(),
            sig_basestring.encode(),
            hashlib.sha256,
        ).hexdigest()
    )

    return hmac.compare_digest(my_signature, signature)
