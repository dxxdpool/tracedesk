"""Browse a catalog of books one page at a time."""

from shelf.paginate import page_count, paginate


def browse(books: list[dict], page: int, per_page: int = 10) -> dict:
    """One page of the catalog, sorted by title, with paging info."""
    ordered = sorted(books, key=lambda book: book["title"].lower())
    return {
        "page": page,
        "pages": page_count(len(ordered), per_page),
        "books": paginate(ordered, page, per_page),
    }
