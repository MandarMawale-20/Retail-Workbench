"""Role catalog loaded from `config/roles.yaml`."""
from __future__ import annotations

from dataclasses import dataclass, field

from app.config import load_yaml


@dataclass(frozen=True)
class RoleSpec:
    name: str
    aliases: tuple[str, ...]
    types: tuple[str, ...]
    negative_aliases: tuple[str, ...] = ()
    qualified_aliases: dict[str, tuple[str, ...]] = field(default_factory=dict)
    distribution: str = "text"


ID_ROLES = frozenset({"product_id", "order_id", "customer_id", "store_id"})
NUMERIC_ROLES = frozenset({"quantity", "unit_price", "line_amount", "available_stock", "reorder_level",
                           "returned_quantity", "return_amount", "unit_cost"})
CATEGORICAL_ROLES = frozenset({"category", "region", "store_name", "return_status"})


def load_roles() -> dict[str, RoleSpec]:
    roles = {}
    for name, spec in load_yaml("roles.yaml").items():
        roles[name] = RoleSpec(
            name=name,
            aliases=tuple(spec["aliases"]),
            types=tuple(spec["types"]),
            negative_aliases=tuple(spec.get("negative_aliases", [])),
            qualified_aliases={k: tuple(v) for k, v in spec.get("qualified_aliases", {}).items()},
            distribution=spec.get("distribution", "text"),
        )
    return roles
