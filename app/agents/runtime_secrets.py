"""Remove per-run transport credentials before messages enter durable traces."""
from dataclasses import fields, is_dataclass, replace


def redact(value, secrets):
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[redacted]")
        return value
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: redact(item, secrets) for key, item in value.items()}
    if is_dataclass(value) and not isinstance(value, type):
        return replace(value, **{field.name: redact(getattr(value, field.name), secrets) for field in fields(value) if field.init})
    return value
