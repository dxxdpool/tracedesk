"""Shopping carts."""


def new_cart(owner: str, items: list[tuple[str, int]] | None = None) -> dict:
    """A cart for `owner`, optionally pre-filled with (name, quantity) pairs."""
    if items is None:
        items = []
    return {"owner": owner, "items": items}


def add(cart: dict, name: str, quantity: int = 1) -> dict:
    """Add `quantity` of `name` to the cart and return it."""
    if quantity < 1:
        raise ValueError("quantity must be at least 1")
    cart["items"].append((name, quantity))
    return cart
