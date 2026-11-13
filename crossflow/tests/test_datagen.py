"""E4 - pilot branch test data."""
import csv
from collections import defaultdict
from decimal import Decimal

import pytest

from crossflow.datagen import generate_pilot_branch

PRODUCT_COLUMNS = {
    "product_id", "sku", "product_name", "category", "subcategory", "brand", "production_date", "expiration_date",
    "unit_price", "cost_price", "supplier_id", "supplier_name", "stock_quantity", "minimum_stock_level",
    "branch_id", "branch_name", "demand_level", "updated_at",
}
SALES_COLUMNS = {
    "sale_line_id", "transaction_id", "date", "time", "branch_id", "product_id", "sku", "product_name",
    "quantity", "unit_price", "total_amount", "payment_method", "offer_id", "discount_amount",
}


@pytest.fixture(scope="module")
def pilot():
    return generate_pilot_branch(7)


def test_same_seed_same_data_other_seed_other_data():
    a, b, c = generate_pilot_branch(3), generate_pilot_branch(3), generate_pilot_branch(4)
    assert a.sales == b.sales and a.products == b.products
    assert a.sales != c.sales


def test_rows_match_the_shared_schema(pilot):
    assert set(pilot.products[0]) == PRODUCT_COLUMNS
    assert set(pilot.sales[0]) == SALES_COLUMNS
    assert all(p["demand_level"] in {"Low", "Medium", "High"} for p in pilot.products)
    assert all(line["payment_method"] in {"cash", "card", "wallet"} for line in pilot.sales)
    assert all(line["quantity"] > 0 for line in pilot.sales)


def test_one_pilot_branch_with_every_named_case(pilot):
    assert {p["branch_id"] for p in pilot.products} == {1}
    assert set(pilot.cases) == {
        "near_expiry_overstock", "near_expiry_low_stock", "high_demand_low_stock", "medium_demand_low_stock",
        "low_demand_low_stock", "low_demand_near_expiry", "underrated_seller",
    }


def test_money_adds_up_on_every_line(pilot):
    for line in pilot.sales:
        assert line["total_amount"] == line["unit_price"] * line["quantity"] - line["discount_amount"]
        assert line["total_amount"].as_tuple().exponent == -2


def test_batches_add_up_to_stock_and_expiry_is_the_earliest_batch(pilot):
    for product in pilot.products:
        batches = pilot.batches_of(product["product_id"])
        assert sum(b["stock_quantity"] for b in batches) == product["stock_quantity"]
        assert product["expiration_date"] == min(b["expiration_date"] for b in batches)


def test_inventory_movements_reconcile_to_the_final_stock(pilot):
    net = defaultdict(int)
    for movement in pilot.movements:
        net[movement["product_id"]] += movement["quantity_change"]
    for product in pilot.products:
        assert net[product["product_id"]] == product["stock_quantity"]


def test_sales_history_ends_the_day_before_the_run(pilot):
    assert max(line["date"] for line in pilot.sales) < pilot.as_of
    assert all(r["returned_at"].date() <= pilot.as_of for r in pilot.returns)


def test_csv_files_use_the_upload_script_names(pilot, tmp_path):
    names = {p.name for p in pilot.write_csv(tmp_path)}
    assert {"supabase_01_branches.csv", "supabase_03_products_all.csv", "sales_transactions_Maadi.csv"} <= names
    with (tmp_path / "sales_transactions_Maadi.csv").open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == len(pilot.sales)
    assert Decimal(rows[0]["total_amount"]) == pilot.sales[0]["total_amount"]


def test_too_few_products_for_the_named_cases():
    with pytest.raises(ValueError):
        generate_pilot_branch(1, n_products=3)
