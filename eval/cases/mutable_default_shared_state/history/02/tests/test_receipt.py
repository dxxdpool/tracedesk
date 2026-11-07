from cart.cart import new_cart
from cart.receipt import receipt_lines


def test_receipt_has_one_line_per_item():
    cart = new_cart("dee", [("milk", 2), ("apple", 1)])
    assert receipt_lines(cart) == ["milk x2 = 240", "apple x1 = 50"]
