_REQUIRED = [
    "name",
    "slug",
    "one_liner",
    "problem",
    "users",
    "auth",
    "features",
    "data_model",
    "stack",
]


def check_completeness(draft_spec: dict) -> list[str]:
    """Return the list of required field names that are missing or empty."""
    missing = []
    for field in _REQUIRED:
        value = draft_spec.get(field)
        if value is None or value == "" or value == []:
            missing.append(field)
    return missing
