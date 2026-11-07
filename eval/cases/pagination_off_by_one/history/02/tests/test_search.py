from shelf.search import by_author

BOOKS = [
    {"title": "Emma", "author": "Jane Austen"},
    {"title": "Persuasion", "author": "Jane Austen"},
    {"title": "Carrie", "author": "Stephen King"},
]


def test_by_author_ignores_case_and_spaces():
    assert [book["title"] for book in by_author(BOOKS, "  austen ")] == ["Emma", "Persuasion"]


def test_by_author_without_match_is_empty():
    assert by_author(BOOKS, "Tolkien") == []
