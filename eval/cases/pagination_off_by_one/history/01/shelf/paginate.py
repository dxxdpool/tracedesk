"""Split a list into pages. Pages are numbered from 1."""


def page_count(total: int, per_page: int) -> int:
    """How many pages are needed for `total` items."""
    if per_page < 1:
        raise ValueError("per_page must be at least 1")
    return (total + per_page - 1) // per_page


def paginate(items: list, page: int, per_page: int = 10) -> list:
    """Return the items on `page` (1-based)."""
    if page < 1:
        raise ValueError("page must be at least 1")
    start = (page - 1) * per_page
    return items[start : start + per_page]
