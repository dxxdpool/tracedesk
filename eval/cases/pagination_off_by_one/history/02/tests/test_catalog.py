from shelf.catalog import browse

BOOKS = [
    {"title": "Dune", "author": "Frank Herbert"},
    {"title": "Emma", "author": "Jane Austen"},
    {"title": "Ivanhoe", "author": "Walter Scott"},
    {"title": "Beloved", "author": "Toni Morrison"},
    {"title": "Carrie", "author": "Stephen King"},
]


def titles(result: dict) -> list[str]:
    return [book["title"] for book in result["books"]]


def test_first_page_starts_with_first_title():
    assert titles(browse(BOOKS, page=1, per_page=2)) == ["Beloved", "Carrie"]


def test_last_page_has_remaining_titles():
    assert titles(browse(BOOKS, page=3, per_page=2)) == ["Ivanhoe"]


def test_page_count_rounds_up():
    assert browse(BOOKS, page=1, per_page=2)["pages"] == 3
