from cart.cart import add, new_cart
from cart.pricing import discounted, total


def test_new_carts_start_empty():
    first = add(new_cart("ana"), "apple", 2)
    second = new_cart("ben")
    assert first["items"] == [("apple", 2)]
    assert second["items"] == []


def test_total_in_cents():
    cart = add(add(new_cart("cy"), "apple", 2), "bread")
    assert total(cart) == 350


def test_discount_rounds_down():
    assert discounted(999, 10) == 899
    assert discounted(1000, 0) == 1000
    assert discounted(1, 50) == 0
