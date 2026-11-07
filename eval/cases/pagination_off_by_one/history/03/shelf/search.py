"""Find books by author."""


def by_author(books: list[dict], name: str) -> list[dict]:
    """Books whose author contains `name`, ignoring case and surrounding spaces."""
    needle = name.strip().lower()
    return [book for book in books if needle in book["author"].lower()]
