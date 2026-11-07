"""Prices and discounts, in cents."""

PRICES = {"apple": 50, "bread": 250, "milk": 120}


def total(cart: dict) -> int:
    return sum(PRICES[name] * quantity for name, quantity in cart["items"])


def discounted(total_cents: int, percent: int) -> int:
    """Total after a percentage discount, rounded down to whole cents."""
    if not 0 <= percent <= 100:
        raise ValueError("percent must be between 0 and 100")
    return total_cents * (100 - percent) // 100
