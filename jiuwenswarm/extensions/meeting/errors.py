"""User-facing exception details for meeting transport boundaries."""
import re


def exception_detail(error: BaseException) -> str:
    text = str(error).strip()
    if not text:
        text = "operation timed out before completion" if isinstance(error, TimeoutError) else "exception supplied no message"
    detail = f"{type(error).__name__}: {text}"
    detail = re.sub(r"Bearer\s+[^\s\"']+", "Bearer [redacted]", detail, flags=re.I)
    detail = re.sub(r"([?&](?:api[_-]?key|token|secret|access_token|app_secret)=)[^&\s]+", r"\1[redacted]", detail, flags=re.I)
    return detail[:2000]
