"""Cart service — add, view, remove, checkout, confirm order."""
import json
import re
import uuid
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import CartItem, Medication
from app.services import llm
from app.services.medication_service import TABLETS_PER_STRIP, get_medication
from app.telegram_format import esc

PLATFORMS = ["1mg", "PharmEasy", "Apollo"]
PLATFORM_URLS = {
    "1mg": "https://www.1mg.com/search/all?name={name}",
    "PharmEasy": "https://pharmeasy.in/search/all?name={name}",
    "Apollo": "https://www.apollopharmacy.in/search-medicines/{name}",
}

# ── Single medicine price prompt (for Buy Now) ──────────────────────────────

SINGLE_PRICE_PROMPT = """You are a pharmacy price assistant for India.

For the medicine: {medicine_name}

Provide the approximate current sticker price (INR) per strip on each platform:
- 1mg (Tata 1mg)
- PharmEasy
- Apollo Pharmacy

Return ONLY valid JSON:
{{"1mg": "₹45", "PharmEasy": "₹48", "Apollo": "₹50"}}

Use "N/A" if unknown. No explanation."""

# ── Cart comparison prompt (for View Cart / Checkout) ───────────────────────

CART_PRICE_PROMPT = """You are a Pharmacy Price Comparison Assistant for India.

Medicines to price (exact brands/dosages only, no alternatives):
{medicine_list}

TASK:
1. List the sticker price per strip for each medicine on: 1mg, PharmEasy, Apollo Pharmacy
2. Calculate the total sticker price for each platform (sum of all medicines × quantity)
3. List any known platform-wide discount offers (e.g. "1mg: 18% off on cart above ₹500")
4. Calculate the updated total after applying the best available offer per platform
5. Identify the cheapest platform for the full order

Return ONLY valid JSON in this exact format:
{{
  "medicines": [
    {{
      "name": "Crocin 650mg",
      "qty": 2,
      "prices": {{"1mg": "₹45", "PharmEasy": "₹48", "Apollo": "₹50"}}
    }}
  ],
  "totals": {{"1mg": "₹90", "PharmEasy": "₹96", "Apollo": "₹100"}},
  "offers": {{"1mg": "18% off above ₹500", "PharmEasy": "15% off first order", "Apollo": "10% off"}},
  "updated_totals": {{"1mg": "₹74", "PharmEasy": "₹82", "Apollo": "₹90"}},
  "best_platform": "1mg"
}}

Use "N/A" for unknown prices. No explanation outside JSON."""


async def _fetch_single_price(medicine_name: str) -> dict[str, str]:
    try:
        prompt = SINGLE_PRICE_PROMPT.format(medicine_name=medicine_name)
        text = re.sub(r"^```(?:json)?\s*", "", (await llm.generate_text(prompt)).strip())
        text = re.sub(r"\s*```$", "", text)
        return json.loads(text)
    except Exception:
        return {p: "N/A" for p in PLATFORMS}


async def _fetch_cart_prices(medicines: list[dict]) -> dict:
    """medicines = [{"name": "Crocin", "qty": 2}, ...]"""
    try:
        med_list = "\n".join(f"- {m['name']} × {m['qty']} strip(s)" for m in medicines)
        prompt = CART_PRICE_PROMPT.format(medicine_list=med_list)
        text = re.sub(r"^```(?:json)?\s*", "", (await llm.generate_text(prompt)).strip())
        text = re.sub(r"\s*```$", "", text)
        return json.loads(text)
    except Exception:
        return {}


def _platform_link(platform: str, medicine_name: str) -> str:
    encoded = quote(medicine_name)
    url = PLATFORM_URLS[platform].format(name=encoded)
    return f'<a href="{url}">{platform}</a>'


async def format_buy_links(medicine_name: str) -> str:
    """Single medicine: prices sorted cheapest first + buy links."""
    prices = await _fetch_single_price(medicine_name)

    platform_data = []
    for platform in PLATFORMS:
        price_str = prices.get(platform, "N/A")
        try:
            price_val = float(price_str.replace("₹", "").replace(",", "").strip())
        except Exception:
            price_val = float("inf")
        platform_data.append((platform, price_str, price_val))

    platform_data.sort(key=lambda x: x[2])

    lines = [f"🛍️ <b>Buy {esc(medicine_name)}:</b>\n"]
    for i, (platform, price_str, _) in enumerate(platform_data):
        tag = " 🏆" if i == 0 and platform_data[0][2] != float("inf") else ""
        link = _platform_link(platform, medicine_name)
        lines.append(f"• {link} — {esc(price_str)}{tag}")

    lines.append("\n<i>Prices are approximate. Click to verify current price.</i>")
    return "\n".join(lines)


async def format_cart_comparison(medicines: list[dict]) -> str:
    """Full cart: comparison table + totals + offers + best platform."""
    data = await _fetch_cart_prices(medicines)
    if not data:
        return "Could not fetch price comparison. Please check platforms directly."

    lines = ["💊 <b>Price Comparison:</b>\n"]

    # Per-medicine prices
    for med in data.get("medicines", []):
        name = med.get("name", "")
        qty = med.get("qty", 1)
        prices = med.get("prices", {})
        price_parts = " | ".join(f"{p}: {esc(prices.get(p, 'N/A'))}" for p in PLATFORMS)
        lines.append(f"• <b>{esc(name)}</b> ×{qty}\n  {price_parts}")

    # Total sticker prices
    totals = data.get("totals", {})
    if totals:
        lines.append("\n📊 <b>Total Sticker Price:</b>")
        for p in PLATFORMS:
            lines.append(f"  {p}: {esc(totals.get(p, 'N/A'))}")

    # Platform offers
    offers = data.get("offers", {})
    if offers:
        lines.append("\n🏷️ <b>Platform Offers:</b>")
        for p in PLATFORMS:
            offer = offers.get(p)
            if offer and offer != "N/A":
                lines.append(f"  {p}: {esc(offer)}")

    # Updated totals after offers
    updated = data.get("updated_totals", {})
    if updated:
        lines.append("\n✅ <b>Updated Total (after offers):</b>")
        for p in PLATFORMS:
            lines.append(f"  {p}: {esc(updated.get(p, 'N/A'))}")

    # Best platform
    best = data.get("best_platform")
    if best in PLATFORM_URLS:
        lines.append(f"\n🏆 <b>Best deal: Order everything from {esc(best)}</b>")
        # Add buy links for each medicine on best platform
        lines.append(f"\n🛒 <b>Buy links on {best}:</b>")
        for med in medicines:
            link = _platform_link(best, med["name"])
            lines.append(f"  • {link}")

    lines.append("\n<i>Prices are approximate. Verify before ordering.</i>")
    return "\n".join(lines)


# ── Cart DB operations ───────────────────────────────────────────────────────

async def add_to_cart(db: AsyncSession, user_id: uuid.UUID, medicine_name: str, quantity: int = 1) -> CartItem | None:
    med = await get_medication(db, user_id, medicine_name)
    if med is None:
        return None

    existing = await db.execute(
        select(CartItem).where(
            CartItem.user_id == user_id,
            CartItem.medication_id == med.id,
            CartItem.status == "active",
        )
    )
    item = existing.scalar_one_or_none()
    if item:
        item.quantity += quantity
        await db.commit()
        await db.refresh(item)
        return item

    item = CartItem(user_id=user_id, medication_id=med.id, quantity=quantity)
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return item


async def remove_from_cart(db: AsyncSession, user_id: uuid.UUID, medicine_name: str) -> bool:
    med = await get_medication(db, user_id, medicine_name)
    if med is None:
        return False

    result = await db.execute(
        select(CartItem).where(
            CartItem.user_id == user_id,
            CartItem.medication_id == med.id,
            CartItem.status == "active",
        )
    )
    item = result.scalar_one_or_none()
    if item:
        item.status = "removed"
        await db.commit()
        return True
    return False


async def get_cart_items(db: AsyncSession, user_id: uuid.UUID) -> list[dict]:
    result = await db.execute(
        select(CartItem, Medication)
        .join(Medication, CartItem.medication_id == Medication.id)
        .where(CartItem.user_id == user_id, CartItem.status == "active")
    )
    rows = result.all()
    return [{"name": med.name, "qty": cart_item.quantity, "medication_id": med.id}
            for cart_item, med in rows]


async def confirm_order(db: AsyncSession, user_id: uuid.UUID, medicine_name: str, quantity: int) -> Medication | None:
    med = await get_medication(db, user_id, medicine_name)
    if med is None:
        return None

    cart_result = await db.execute(
        select(CartItem).where(
            CartItem.user_id == user_id,
            CartItem.medication_id == med.id,
            CartItem.status == "active",
        )
    )
    cart_item = cart_result.scalar_one_or_none()
    if cart_item:
        cart_item.status = "ordered"

    tablets = quantity * TABLETS_PER_STRIP
    med.remaining_quantity += tablets
    med.total_quantity += tablets
    await db.commit()
    await db.refresh(med)
    return med
