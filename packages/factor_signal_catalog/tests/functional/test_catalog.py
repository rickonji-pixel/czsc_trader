from __future__ import annotations

from pathlib import Path

import pytest

from factor_signal_catalog import CatalogRegistry, CatalogValidationError, FactorDefinition


REPO = Path(__file__).resolve().parents[4]


def test_fsc01_repository_catalog_is_complete_queryable_and_strict() -> None:
    catalog = CatalogRegistry(REPO / "catalog")
    assert len(catalog.families) >= 8
    assert len(catalog.factors) >= 10
    assert len(catalog.signals) >= 246
    assert len(catalog.digest) == 64
    assert catalog.show("F-PROJECT-ER60")["information_family"] == "TREND_REGIME"
    assert "T-1" in catalog.show("F-PROJECT-ER60")["causality"]
    assert catalog.show("F-PROJECT-BREADTH-BALANCE")["implementation"].endswith(
        "build_weighted_market_breadth_features"
    )
    assert catalog.show("F-PROJECT-ETF-NAV-PREMIUM")["inputs"] == [
        "etf_share_size.nav",
        "etf_share_size.close",
    ]
    assert any(
        row["id"] == "SIG-CZSC-cxt_bi_base_V230228"
        for row in catalog.list_definitions(kind="signal", family="MARKET_STRUCTURE")
    )

    with pytest.raises(CatalogValidationError, match="missing fields"):
        FactorDefinition.from_dict({"factor_id": "F-BROKEN"})
    with pytest.raises(CatalogValidationError, match="unknown information family"):
        catalog.list_definitions(family="MISSING")
