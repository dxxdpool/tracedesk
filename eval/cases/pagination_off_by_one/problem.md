Catalog browsing has been wrong since the latest refactor.

With 2 books per page, page 1 should list the first two titles alphabetically ("Beloved", "Carrie"),
but it shows "Dune" and "Emma" instead. The last page of the catalog comes back empty. The test suite
has started failing too.

Repo: eval/cases/pagination_off_by_one/repo
