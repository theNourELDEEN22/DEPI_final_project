"""E4 - Test data generator for sales, stock and expiry: one pilot branch (Epic E).

    data = generate_pilot_branch(seed=7)
    data.write_csv("out/")        # same file names as foundation/db/upload_to_supabase.py

Deterministic: the same seed and settings give byte-identical data.

Rows follow the shared schema (branches, suppliers, products, product_batches,
sales_transactions, returns, inventory_movements). On top of normal products it
always contains NAMED CASES whose expected routing is known, built from the real
restock_thresholds seeded in the migration (so a config change keeps them valid):

    near_expiry_overstock    expiring, stock above threshold     -> offer, not restock (E7)
    near_expiry_low_stock    expiring, stock at/below threshold  -> restock NEAR_EXPIRY
    high_demand_low_stock    High demand, low stock              -> restock
    medium_demand_low_stock  Medium demand, low stock            -> restock
    low_demand_low_stock     Low demand, low stock               -> nothing (B6)
    low_demand_near_expiry   Low demand, expiring                -> open decision (E1, D-06)
    underrated_seller        labeled Medium, sells like High     -> demand-level proposal (E12)

Sales history covers [as_of - history_days, as_of) with a day-of-week pattern
(Thursday to Saturday busier, as in Egypt), receipts of 1-4 lines, cash/card/wallet,
~1% returns, and inventory movements that reconcile to the final stock.
"""
from __future__ import annotations

import csv
import math
import random
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from offers.guardrails import InMemoryPrices, InMemoryProductRules, PriceInfo
from offers.validation import InMemoryCatalog
from restocking.config import category_thresholds
from crossflow.seed_config import restock_settings

CAIRO = ZoneInfo("Africa/Cairo")
TWO_PLACES = Decimal("0.01")

# category -> (min price, max price, fresh shelf life in days (min, max), names)
CATEGORIES: dict[str, tuple[int, int, tuple[int, int], tuple[str, ...]]] = {
    "Bakery": (5, 40, (3, 6), ("Baladi Bread", "Croissant", "Toast Loaf", "Feteer")),
    "Dairy": (12, 120, (8, 20), ("Milk 1L", "Yogurt Cup", "Feta Cheese", "Laban Rayeb")),
    "Frozen Food": (45, 260, (90, 270), ("Frozen Chicken", "Frozen Peas", "Molokhia Pack")),
    "Beverages": (8, 60, (120, 300), ("Mango Juice", "Mineral Water 1.5L", "Cola Can")),
    "Snacks": (5, 45, (60, 180), ("Chips", "Biscuits", "Chocolate Bar")),
    "Staples": (20, 150, (180, 540), ("Rice 1kg", "Pasta 400g", "Lentils 1kg", "Sugar 1kg")),
    "Cleaning": (25, 180, (360, 720), ("Dish Soap", "Laundry Powder", "Floor Cleaner")),
    "Home": (30, 250, (360, 720), ("Kitchen Towels", "Trash Bags", "Foil Roll")),
    "Personal Care": (20, 200, (360, 720), ("Shampoo", "Toothpaste", "Soap Bar")),
    "School Supplies": (5, 90, (720, 1095), ("Notebook", "Pen Pack", "Pencil Case")),
}

DAILY_RATE = {"High": 9.0, "Medium": 4.0, "Low": 1.0}  # mean units per day
WEEKDAY_FACTOR = {0: 1.0, 1: 0.95, 2: 0.95, 3: 1.15, 4: 1.25, 5: 1.1, 6: 1.0}  # Mon..Sun
PAYMENT_METHODS = ("cash", "card", "wallet")
RETURN_RATE = 0.01


@dataclass
class PilotData:
    as_of: date
    settings: dict[str, Any]
    branch: dict[str, Any]
    suppliers: list[dict[str, Any]]
    products: list[dict[str, Any]]
    batches: list[dict[str, Any]]
    sales: list[dict[str, Any]]
    returns: list[dict[str, Any]]
    movements: list[dict[str, Any]]
    cases: dict[str, int] = field(default_factory=dict)  # case name -> product_id

    @property
    def branch_id(self) -> int:
        return int(self.branch["branch_id"])

    def product(self, product_id: int) -> dict[str, Any]:
        return next(p for p in self.products if p["product_id"] == product_id)

    def batches_of(self, product_id: int) -> list[dict[str, Any]]:
        return [b for b in self.batches if b["product_id"] == product_id]

    # --- views the other workflows read ---------------------------------------------------

    def restock_inventory(self) -> list[dict[str, Any]]:
        """Rows in the shape SupabaseRestockRepository.load_inventory returns."""
        rows = []
        for product in self.products:
            batches = [
                {
                    "batch_id": b["batch_id"],
                    "expiration_date": b["expiration_date"].isoformat(),
                    "stock_quantity": b["stock_quantity"],
                }
                for b in self.batches_of(product["product_id"])
            ]
            on_hand = sum(
                b["stock_quantity"] for b in self.batches_of(product["product_id"])
                if b["expiration_date"] >= self.as_of
            )
            rows.append({**_plain(product), "on_hand": on_hand, "batches": batches})
        return rows

    def catalog(self) -> InMemoryCatalog:
        return InMemoryCatalog({str(self.branch_id): {str(p["product_id"]) for p in self.products}})

    def prices(self) -> InMemoryPrices:
        return InMemoryPrices(
            {
                (str(self.branch_id), str(p["product_id"])): PriceInfo(price=p["unit_price"], cost=p["cost_price"])
                for p in self.products
            }
        )

    def product_rules(self) -> InMemoryProductRules:
        """Earliest unexpired batch per product, as the start of that day in Cairo."""
        expiry = {}
        for p in self.products:
            dates = [b["expiration_date"] for b in self.batches_of(p["product_id"])
                     if b["expiration_date"] >= self.as_of and b["stock_quantity"] > 0]
            if dates:
                expiry[(str(self.branch_id), str(p["product_id"]))] = datetime.combine(min(dates), time.min, CAIRO)
        return InMemoryProductRules(expiry=expiry)

    # --- export ------------------------------------------------------------------------------

    def write_csv(self, out_dir: str | Path) -> list[Path]:
        """Write CSVs named like foundation/db/upload_to_supabase.py expects."""
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        name = self.branch["branch_name"].replace(" Branch", "").replace(" ", "_")
        tables = [
            ("supabase_01_branches.csv", [self.branch]),
            ("supabase_02_suppliers.csv", self.suppliers),
            ("supabase_03_products_all.csv", self.products),
            ("supabase_04_product_batches.csv", self.batches),
            (f"sales_transactions_{name}.csv", self.sales),
            ("supabase_05_returns.csv", self.returns),
            ("supabase_06_inventory_movements.csv", self.movements),
        ]
        written = []
        for filename, rows in tables:
            path = out / filename
            with path.open("w", newline="", encoding="utf-8") as handle:
                if rows:
                    writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                    writer.writeheader()
                    writer.writerows({k: _csv_value(v) for k, v in row.items()} for row in rows)
            written.append(path)
        return written


def _csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def _plain(row: dict[str, Any]) -> dict[str, Any]:
    """Dates as ISO text, like rows read back from the database."""
    return {k: (v.isoformat() if isinstance(v, (date, datetime)) else v) for k, v in row.items()}


def _poisson(rng: random.Random, lam: float) -> int:
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def _money(value) -> Decimal:
    return Decimal(value).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class _Spec:
    case: Optional[str]
    category: str
    demand: str
    sells_like: str
    stock: int
    expiring_in: Optional[int]  # days until the expiring batch expires; None = all fresh
    expiring_qty: int


def _case_specs(settings: dict[str, Any]) -> list[_Spec]:
    def th(category: str) -> dict[str, Any]:
        return category_thresholds(settings, category, "")

    dairy, snacks, drinks, bakery, staples = th("Dairy"), th("Snacks"), th("Beverages"), th("Bakery"), th("Staples")
    return [
        _Spec("near_expiry_overstock", "Dairy", "High", "High",
              dairy["high_demand_qty"] + 70, dairy["expiry_days"], 40),
        _Spec("near_expiry_low_stock", "Dairy", "High", "High",
              max(2, dairy["high_demand_qty"] // 2), dairy["expiry_days"], max(1, dairy["high_demand_qty"] // 4)),
        _Spec("high_demand_low_stock", "Snacks", "High", "High", max(1, snacks["high_demand_qty"] // 2), None, 0),
        _Spec("medium_demand_low_stock", "Beverages", "Medium", "Medium",
              max(1, drinks["medium_demand_qty"] // 2), None, 0),
        _Spec("low_demand_low_stock", "Cleaning", "Low", "Low", 2, None, 0),
        _Spec("low_demand_near_expiry", "Bakery", "Low", "Low", 30, max(0, bakery["expiry_days"] - 1), 30),
        _Spec("underrated_seller", "Staples", "Medium", "High", staples["medium_demand_qty"] + 60, None, 0),
    ]


def _normal_spec(rng: random.Random, settings: dict[str, Any]) -> _Spec:
    category = rng.choice(sorted(CATEGORIES))
    demand = rng.choices(("High", "Medium", "Low"), weights=(3, 5, 2))[0]
    rules = category_thresholds(settings, category, "")
    if demand == "High":
        stock = rules["high_demand_qty"] + rng.randint(15, 80)
    elif demand == "Medium":
        stock = rules["medium_demand_qty"] + rng.randint(15, 80)
    else:
        stock = rng.randint(5, 60)
    return _Spec(None, category, demand, demand, stock, None, 0)


def generate_pilot_branch(
    seed: int = 7,
    *,
    as_of: date = date(2026, 11, 10),
    history_days: int = 56,
    n_products: int = 40,
    settings: dict[str, Any] | None = None,
    branch_id: int = 1,
    branch_name: str = "Maadi Branch",
) -> PilotData:
    """Generate one pilot branch. `as_of` is the business date of the next nightly run."""
    settings = settings or restock_settings()
    rng = random.Random(seed)
    specs = _case_specs(settings)
    if n_products < len(specs):
        raise ValueError(f"n_products must be at least {len(specs)} (one per named case)")
    specs += [_normal_spec(rng, settings) for _ in range(n_products - len(specs))]

    branch = {"branch_id": branch_id, "branch_name": branch_name, "city": "Cairo",
              "timezone": "Africa/Cairo", "is_active": True}

    suppliers, supplier_of = [], {}
    for i, category in enumerate(sorted(CATEGORIES), start=1):
        slug = category.lower().replace(" ", "-")
        supplier = {
            "supplier_id": 100 + i,
            "supplier_name": f"{category} Supplies Co.",
            "product_category": category,
            "supplier_location": rng.choice(("Cairo", "Giza", "10th of Ramadan", "Alexandria")),
            "contact_name": f"Sales desk {i}",
            "contact_phone": f"+20 10 {rng.randint(1000, 9999)} {rng.randint(1000, 9999)}",
            "shipping_duration_days": rng.randint(1, 5),
            "minimum_order_quantity": rng.choice((5, 10, 12, 20)),
            "email": f"orders+{slug}@supplier-demo.eg",
        }
        suppliers.append(supplier)
        supplier_of[category] = supplier

    products, batches, cases = [], [], {}
    batch_seq = 0
    for index, spec in enumerate(specs, start=1):
        pid = index
        low, high, (shelf_min, shelf_max), names = CATEGORIES[spec.category]
        price = _money(rng.uniform(low, high))
        cost = _money(price * Decimal(str(round(rng.uniform(0.58, 0.78), 2))))
        rules = category_thresholds(settings, spec.category, "")
        name = f"{rng.choice(names)} #{pid}"

        product_batches = []
        if spec.expiring_in is not None and spec.expiring_qty > 0:
            expiring_qty = min(spec.expiring_qty, spec.stock)
            product_batches.append((as_of + timedelta(days=spec.expiring_in), expiring_qty))
            if spec.stock - expiring_qty > 0:
                fresh = as_of + timedelta(days=rules["expiry_days"] + rng.randint(5, max(6, shelf_max)))
                product_batches.append((fresh, spec.stock - expiring_qty))
        else:
            first = as_of + timedelta(days=rules["expiry_days"] + rng.randint(3, max(4, shelf_max)))
            if spec.stock > 20 and rng.random() < 0.5:
                split = rng.randint(1, spec.stock - 1)
                second = first + timedelta(days=rng.randint(2, 30))
                product_batches += [(first, split), (second, spec.stock - split)]
            else:
                product_batches.append((first, spec.stock))

        for expires_on, quantity in product_batches:
            batch_seq += 1
            batches.append({
                "batch_id": batch_seq,
                "batch_number": f"B{branch_id:02d}-{pid:04d}-{batch_seq:05d}",
                "branch_id": branch_id,
                "product_id": pid,
                "production_date": expires_on - timedelta(days=rng.randint(shelf_min, shelf_max)),
                "expiration_date": expires_on,
                "stock_quantity": quantity,
                "cost_price": cost,
            })
        earliest = min(e for e, _ in product_batches)
        supplier = supplier_of[spec.category]
        products.append({
            "product_id": pid,
            "sku": f"SKU-{branch_id:02d}-{pid:04d}",
            "product_name": name,
            "category": spec.category,
            "subcategory": "",
            "brand": rng.choice(("Juhayna", "Domty", "Edita", "Faragello", "Al Doha", "Persil", "Bic")),
            "production_date": earliest - timedelta(days=shelf_max),
            "expiration_date": earliest,
            "unit_price": price,
            "cost_price": cost,
            "supplier_id": supplier["supplier_id"],
            "supplier_name": supplier["supplier_name"],
            "stock_quantity": spec.stock,
            "minimum_stock_level": rules["medium_demand_qty"],
            "branch_id": branch_id,
            "branch_name": branch_name,
            "demand_level": spec.demand,
            "updated_at": datetime.combine(as_of, time.min, CAIRO).astimezone(timezone.utc),
        })
        if spec.case:
            cases[spec.case] = pid

    sales, returns = _simulate_sales(rng, products, specs, branch_id, as_of, history_days)
    movements = _movements(products, specs, sales, returns, branch_id, as_of, history_days)
    return PilotData(as_of, settings, branch, suppliers, products, batches, sales, returns, movements, cases)


def _simulate_sales(rng, products, specs, branch_id, as_of, history_days):
    sales, returns = [], []
    line_id, txn_seq, return_id = 0, 0, 0
    for offset in range(history_days, 0, -1):
        day = as_of - timedelta(days=offset)
        items = []
        for product, spec in zip(products, specs):
            units = _poisson(rng, DAILY_RATE[spec.sells_like] * WEEKDAY_FACTOR[day.weekday()])
            while units > 0:
                qty = min(units, rng.choice((1, 1, 1, 2, 3)))
                items.append((product, qty))
                units -= qty
        rng.shuffle(items)
        while items:
            size = rng.randint(1, 4)
            receipt, items = items[:size], items[size:]
            txn_seq += 1
            transaction_id = branch_id * 1_000_000 + txn_seq
            moment = time(rng.randint(8, 22), rng.randint(0, 59), rng.randint(0, 59))
            method = rng.choice(PAYMENT_METHODS)
            for product, qty in receipt:
                line_id += 1
                total = _money(product["unit_price"] * qty)
                line = {
                    "sale_line_id": line_id,
                    "transaction_id": transaction_id,
                    "date": day,
                    "time": moment,
                    "branch_id": branch_id,
                    "product_id": product["product_id"],
                    "sku": product["sku"],
                    "product_name": product["product_name"],
                    "quantity": qty,
                    "unit_price": product["unit_price"],
                    "total_amount": total,
                    "payment_method": method,
                    "offer_id": None,
                    "discount_amount": Decimal("0.00"),
                }
                sales.append(line)
                if rng.random() < RETURN_RATE:
                    return_id += 1
                    returned_at = datetime.combine(day + timedelta(days=1), time(12, 0), CAIRO)
                    if returned_at.date() < as_of:  # only returns that already happened
                        returns.append({
                            "return_id": return_id,
                            "branch_id": branch_id,
                            "transaction_id": transaction_id,
                            "product_id": product["product_id"],
                            "quantity": 1,
                            "refund_amount": _money(total / qty),
                            "reason": rng.choice(("damaged", "expired", "changed mind")),
                            "returned_at": returned_at.astimezone(timezone.utc),
                        })
    return sales, returns


def _movements(products, specs, sales, returns, branch_id, as_of, history_days):
    sold, returned = {}, {}
    for line in sales:
        sold[line["product_id"]] = sold.get(line["product_id"], 0) + line["quantity"]
    for item in returns:
        returned[item["product_id"]] = returned.get(item["product_id"], 0) + item["quantity"]

    movements, seq = [], 0
    start = datetime.combine(as_of - timedelta(days=history_days), time(6, 0), CAIRO)

    def add(product_id, kind, change, reference, at):
        nonlocal seq
        seq += 1
        movements.append({
            "movement_id": seq,
            "branch_id": branch_id,
            "product_id": product_id,
            "batch_id": None,
            "movement_type": kind,
            "quantity_change": change,
            "reference_id": reference,
            "occurred_at": at.astimezone(timezone.utc),
        })

    weeks = max(1, history_days // 7)
    for product, spec in zip(products, specs):
        pid = product["product_id"]
        net_out = sold.get(pid, 0) - returned.get(pid, 0)
        restock_total = max(0, net_out - round(DAILY_RATE[spec.sells_like] * 7))
        opening = product["stock_quantity"] + net_out - restock_total
        add(pid, "ADJUSTMENT", opening, "opening-balance", start)
        base, extra = divmod(restock_total, weeks)
        for week in range(weeks):
            amount = base + (extra if week == weeks - 1 else 0)
            if amount:
                add(pid, "RESTOCK", amount, f"restock-week-{week + 1}", start + timedelta(days=7 * week + 1))

    for line in sales:
        at = datetime.combine(line["date"], line["time"], CAIRO)
        add(line["product_id"], "SALE", -line["quantity"], str(line["transaction_id"]), at)
    for item in returns:
        add(item["product_id"], "RETURN", item["quantity"], f"return-{item['return_id']}", item["returned_at"])
    return movements
