"""Independent pandas oracle. Reads RAW fixtures and applies its own simple, documented cleaning.

Documented cleaning (deliberately simpler than the system's):
 * strip whitespace; '', 'n/a', 'na', 'null', '-' -> missing
 * product ids upper-cased; category aliases tee/tshirt -> T-Shirt, electronic -> Electronics
 * money strings: drop currency symbol and thousands commas; European '1.200,50' style -> missing
 * exact duplicate rows removed after normalisation
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd

_TOKENS = {"", "n/a", "na", "null", "-", "none"}
_CATEGORY = {"tee": "T-Shirt", "t-shirt": "T-Shirt", "tshirt": "T-Shirt", "electronic": "Electronics"}


def _read(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df = df.apply(lambda c: c.str.strip())
    return df.apply(lambda c: c.where(~c.str.lower().isin(_TOKENS), np.nan))


def _money(value) -> float:
    if not isinstance(value, str):
        return np.nan
    text = re.sub(r"[₹$\s]", "", value)
    if not re.fullmatch(r"-?(\d{1,3}(,\d{3})+|\d+)(\.\d+)?", text):   # '1.299,50' style is NOT a number
        return np.nan
    return float(text.replace(",", ""))


class Oracle:
    def __init__(self, data_dir: Path) -> None:
        d = Path(data_dir)
        self.products = _read(d / "products.csv")
        self.products["category"] = self.products["category"].map(lambda v: _CATEGORY.get(str(v).lower(), v))
        self.stores = _read(d / "stores.csv")
        orders = _read(d / "orders.csv")
        orders["product_id"] = orders["product_id"].str.upper()
        self.orders_all_rows = len(orders)
        self.orders = orders.drop_duplicates().reset_index(drop=True)
        self.orders["quantity"] = pd.to_numeric(self.orders["quantity"], errors="coerce")
        self.orders["unit_price"] = self.orders["unit_price"].map(_money)
        returns = _read(d / "returns.csv")
        returns["returned_quantity"] = pd.to_numeric(returns["returned_quantity"], errors="coerce")
        returns["return_amount"] = pd.to_numeric(returns["return_amount"], errors="coerce")
        self.returns = returns
        inventory = _read(d / "inventory.csv")
        inventory["available_stock"] = pd.to_numeric(inventory["available_stock"], errors="coerce")
        self.inventory = inventory

    def _orders_view(self, region: str | None) -> pd.DataFrame:
        o = self.orders.merge(self.stores[["store_id", "region"]], on="store_id", how="left")
        o = o.merge(self.products[["product_id", "category"]], on="product_id", how="left")
        o["revenue"] = o["quantity"] * o["unit_price"]
        return o if region is None else o[o["region"] == region]

    def revenue_by_category(self, region: str | None = None) -> pd.Series:
        o = self._orders_view(region)
        return o.groupby("category")["revenue"].sum().sort_values(ascending=False)

    def gross_revenue(self) -> float:
        return float((self.orders["quantity"] * self.orders["unit_price"]).sum())

    def net_revenue(self) -> float:
        return self.gross_revenue() - float(self.returns["return_amount"].sum())

    def return_rate_by_category(self, region: str | None = None) -> pd.Series:
        o = self._orders_view(region)
        r = self.returns.merge(self.products[["product_id", "category"]], on="product_id", how="left")
        r = r.merge(self.orders[["order_id", "store_id"]], on="order_id", how="left").merge(
            self.stores[["store_id", "region"]], on="store_id", how="left")
        if region is not None:
            r = r[r["region"] == region]
        num = r.groupby("category")["returned_quantity"].sum()
        den = o.groupby("category")["quantity"].sum()
        rate = (100.0 * num.reindex(den.index.union(num.index)).fillna(0) / den.reindex(den.index.union(num.index))
                .replace(0, np.nan)).dropna()
        return rate.sort_values(ascending=False)

    def stock_by_category(self, region: str | None = None) -> pd.Series:
        inv = self.inventory.merge(self.stores[["store_id", "region"]], on="store_id", how="left").merge(
            self.products[["product_id", "category"]], on="product_id", how="left")
        if region is not None:
            inv = inv[inv["region"] == region]
        return inv.groupby("category")["available_stock"].sum().sort_values()

    def avg_unit_price(self) -> float:
        return float(self.orders["unit_price"].mean())

    def naive_fanout_revenue(self, dup_products: pd.DataFrame) -> float:
        """Demonstrates fan-out: merging orders to a product table with duplicate keys inflates revenue."""
        merged = self.orders.merge(dup_products[["product_id", "category"]], on="product_id", how="left")
        return float((merged["quantity"] * merged["unit_price"]).sum())
