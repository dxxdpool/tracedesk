"""Plain-text receipts."""

from cart.pricing import PRICES


def receipt_lines(cart: dict) -> list[str]:
    """One line per item: name, quantity and price in cents."""
    return [f"{name} x{quantity} = {PRICES[name] * quantity}" for name, quantity in cart["items"]]
