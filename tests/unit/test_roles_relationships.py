import pandas as pd

from app.errors import AppError
from app.metrics.binder import bind_metric
from app.profiling.profiler import profile_frame
from app.relationships.discovery import discover_relationships
from app.relationships.join_policy import plan_joins
from app.roles.role_inference import infer_roles


def prof(df, name):
    return profile_frame(df, dataset=name, version_id=f"dv_{name}", stage="clean")


def infer(frames):
    return infer_roles({n: prof(df, n) for n, df in frames.items()}, frames)


def test_roles_work_on_renamed_unseen_schema():
    orders = pd.DataFrame({"Txn No": ["1", "2", "3"], "SKU": ["a", "b", "a"], "Qty Sold": ["1", "2", "3"],
                           "Selling Price": ["10.5", "20", "5"]}, dtype=object)
    roles = {b.role for b in infer({"sales": orders}) if b.status in ("verified", "candidate")}
    assert {"order_id", "product_id", "quantity", "unit_price"} <= roles


def test_roles_cover_unit_suffix_and_multilingual_headers():
    orders = pd.DataFrame({"SKU Code": ["a", "b", "a"], "Qty.": ["1", "2", "3"],
                           "precio_unitario": ["10.5", "20", "5"]}, dtype=object)
    roles = {b.role for b in infer({"ventas": orders}) if b.status in ("verified", "candidate")}
    assert {"product_id", "quantity", "unit_price"} <= roles


def test_return_qty_is_not_quantity():
    df = pd.DataFrame({"return_qty": ["1", "2", "3"], "quantity": ["5", "6", "7"]}, dtype=object)
    by_col = {b.column_id: b.role for b in infer({"r": df})}
    assert by_col["r.return_qty"] == "returned_quantity" and by_col["r.quantity"] == "quantity"


def test_tied_columns_are_ambiguous_not_guessed():
    df = pd.DataFrame({"quantity": ["1", "2", "3"], "qty": ["4", "5", "6"]}, dtype=object)
    statuses = {b.status for b in infer({"o": df}) if b.role == "quantity"}
    assert statuses == {"ambiguous"}
    try:
        bind_metric("units_sold", infer({"o": df}))
    except AppError as exc:
        assert exc.code == "UNSUPPORTED_QUERY" and "quantity" in exc.details["ambiguous_roles"]
    else:
        raise AssertionError("ambiguous role must not bind")


def _graph(products):
    orders = pd.DataFrame({"product_id": ["P1", "P2", "P1", "P9"], "quantity": ["1", "2", "3", "4"]}, dtype=object)
    frames = {"orders": orders, "products": products}
    return discover_relationships(frames, {n: f"dv_{n}" for n in frames}, infer(frames))


def test_verified_many_to_one_and_unsafe_duplicate_keys():
    good = pd.DataFrame({"product_id": ["P1", "P2"], "category": ["A", "B"]}, dtype=object)
    rel = _graph(good).relationships[0]
    assert (rel.cardinality, rel.status, rel.left_dataset) == ("many_to_one", "verified", "orders")
    assert rel.unmatched_left == 1
    dup = pd.DataFrame({"product_id": ["P1", "P2", "P1"], "category": ["A", "B", "C"]}, dtype=object)
    unsafe = _graph(dup)
    assert unsafe.relationships[0].status == "unsafe"
    try:
        plan_joins(unsafe, "orders", {"products"}, None)
    except AppError as exc:
        assert exc.code == "UNSAFE_JOIN"
    else:
        raise AssertionError("unsafe relationship must not be joined")


def test_join_policy_never_traverses_one_to_many():
    good = pd.DataFrame({"product_id": ["P1", "P2"], "category": ["A", "B"]}, dtype=object)
    graph = _graph(good)
    assert [e.to_dataset for e in plan_joins(graph, "orders", {"products"}, None)] == ["products"]
    try:
        plan_joins(graph, "products", {"orders"}, None)     # dimension -> fact would fan out
    except AppError as exc:
        assert exc.code in ("UNKNOWN_RELATIONSHIP", "UNSAFE_JOIN")
    else:
        raise AssertionError("fan-out path must be refused")


def test_retail_metrics_bind_complete_and_report_missing_roles():
    from app.schemas import RoleBinding

    def binding(role, column_id, status="verified"):
        return RoleBinding(role=role, dataset_version_id="v", column_id=column_id, confidence=1.0,
                           inference_source=["alias"], status=status)

    complete = [
        binding("quantity", "orders.quantity"),
        binding("available_stock", "inventory.available_stock"),
        binding("reorder_level", "inventory.reorder_level"),
        binding("inventory_date", "inventory.inventory_date"),
    ]
    assert bind_metric("sell_through", complete).variant_id == "sold_plus_stock"
    assert bind_metric("stock_out_rate", complete).variant_id == "non_positive_stock"
    assert bind_metric("inventory_age_days", complete).variant_id == "inventory_date_age"
    assert bind_metric("stock_risk_score", complete).variant_id == "below_reorder_level"

    try:
        bind_metric("inventory_age_days", complete[:2])
    except AppError as exc:
        assert exc.code == "UNSUPPORTED_QUERY"
        assert exc.details["missing_roles"] == ["inventory_date"]
    else:
        raise AssertionError("inventory age must require an inventory date")
