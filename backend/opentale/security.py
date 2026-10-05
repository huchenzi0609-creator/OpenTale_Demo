"""Credential detection after decoding; never include matched values in diagnostics."""


def contains_credential(value: object, credential: str) -> bool:
    if not credential:
        return False
    if isinstance(value, str):
        return credential in value
    if isinstance(value, dict):
        return any(
            contains_credential(key, credential) or contains_credential(item, credential)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(contains_credential(item, credential) for item in value)
    return False
