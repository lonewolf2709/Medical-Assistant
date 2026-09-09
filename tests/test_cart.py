"""Cart lookups and price-comparison formatting."""
import pytest

from app.services import cart_service, medication_service


async def test_add_to_cart_does_not_treat_the_name_as_a_wildcard(db, user):
    await medication_service.add_or_update_medication(db, user.id, "Crocin", ["morning"], 10)

    assert await cart_service.add_to_cart(db, user.id, "Croc%") is None
    assert await cart_service.add_to_cart(db, user.id, "crocin") is not None


async def test_remove_from_cart_does_not_treat_the_name_as_a_wildcard(db, user):
    await medication_service.add_or_update_medication(db, user.id, "Crocin", ["morning"], 10)
    await cart_service.add_to_cart(db, user.id, "Crocin")

    assert await cart_service.remove_from_cart(db, user.id, "Croc%") is False


async def test_confirm_order_does_not_treat_the_name_as_a_wildcard(db, user):
    await medication_service.add_or_update_medication(db, user.id, "Crocin", ["morning"], 10)

    assert await cart_service.confirm_order(db, user.id, "Croc%", 1) is None


async def test_buy_links_escape_the_medication_name(monkeypatch):
    async def fake_prices(name):
        return {"1mg": "₹45", "PharmEasy": "N/A", "Apollo": "₹50"}

    monkeypatch.setattr(cart_service, "_fetch_single_price", fake_prices)

    out = await cart_service.format_buy_links("Vitamin B<3 & Co")

    assert "B&lt;3 &amp; Co" in out
    assert "B<3" not in out


async def test_cart_comparison_escapes_names_from_the_model(monkeypatch):
    async def fake_cart_prices(medicines):
        return {
            "medicines": [{"name": "Vitamin B<3", "qty": 1, "prices": {"1mg": "₹45"}}],
            "totals": {"1mg": "₹45"},
            "best_platform": "1mg",
        }

    monkeypatch.setattr(cart_service, "_fetch_cart_prices", fake_cart_prices)

    out = await cart_service.format_cart_comparison([{"name": "Vitamin B<3", "qty": 1}])

    assert "B&lt;3" in out
    assert "B<3" not in out
