"""Actions for ResonancePc market data service."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from packages.aura_core.api import action_info, requires_services
from packages.resonance_shared.market_proxy import normalize_market_proxy, validate_market_source

from ..services.resonance_pc_market_data_service import ResonancePcMarketDataService


def _require_service(service: Optional[ResonancePcMarketDataService]) -> ResonancePcMarketDataService:
    if service is None:
        raise RuntimeError("resonance_pc_market_data service is not available.")
    return service


def _market_request_options(proxy_mode: str, port: int | None, source: str) -> dict[str, Any]:
    proxy = normalize_market_proxy(proxy_mode, port)
    settings = proxy if proxy_mode == "manual" else {}
    if validate_market_source(source) != "automatic":
        settings["market_source_mode"] = source
    return settings


@action_info(name="resonance_pc.market_refresh", public=True, read_only=False, description="Refresh ResonancePc market snapshot.")
@requires_services(resonance_pc_market_data="resonance_pc_market_data")
def resonance_pc_market_refresh(
    force: bool = False,
    resonance_pc_market_data: ResonancePcMarketDataService | None = None,
    market_proxy_mode: str = "automatic",
    market_proxy_port: int | None = None,
    market_source_mode: str = "automatic",
) -> Dict[str, Any]:
    options = _market_request_options(market_proxy_mode, market_proxy_port, market_source_mode)
    return _require_service(resonance_pc_market_data).refresh(force=force, **options)


@action_info(
    name="resonance_pc.market_sync_web_constants",
    public=True,
    read_only=False,
    description="Sync route constants (cities/fatigue) from webpage and optionally sync buy_lot.",
)
@requires_services(resonance_pc_market_data="resonance_pc_market_data")
def resonance_pc_market_sync_web_constants(
    sync_buy_lot: bool = True,
    resonance_pc_market_data: ResonancePcMarketDataService | None = None,
) -> Dict[str, Any]:
    return _require_service(resonance_pc_market_data).sync_web_constants(sync_buy_lot=sync_buy_lot)


@action_info(name="resonance_pc.market_get_latest", public=True, read_only=True, description="Get latest ResonancePc market snapshot.")
@requires_services(resonance_pc_market_data="resonance_pc_market_data")
def resonance_pc_market_get_latest(
    resonance_pc_market_data: ResonancePcMarketDataService | None = None,
) -> Dict[str, Any]:
    return _require_service(resonance_pc_market_data).get_latest()


@action_info(name="resonance_pc.market_get_snapshot", public=True, read_only=True, description="Get ResonancePc market snapshot by id.")
@requires_services(resonance_pc_market_data="resonance_pc_market_data")
def resonance_pc_market_get_snapshot(
    snapshot_id: str,
    resonance_pc_market_data: ResonancePcMarketDataService | None = None,
) -> Dict[str, Any]:
    return _require_service(resonance_pc_market_data).get_snapshot(snapshot_id=snapshot_id)


@action_info(name="resonance_pc.market_list_snapshots", public=True, read_only=True, description="List cached ResonancePc market snapshots.")
@requires_services(resonance_pc_market_data="resonance_pc_market_data")
def resonance_pc_market_list_snapshots(
    limit: int = 50,
    resonance_pc_market_data: ResonancePcMarketDataService | None = None,
) -> List[Dict[str, Any]]:
    return _require_service(resonance_pc_market_data).list_snapshots(limit=limit)


@action_info(name="resonance_pc.market_query_products", public=True, read_only=True, description="Query normalized products from latest ResonancePc snapshot.")
@requires_services(resonance_pc_market_data="resonance_pc_market_data")
def resonance_pc_market_query_products(
    scope: Optional[str] = None,
    city_id: Optional[str] = None,
    side: Optional[str] = None,
    resonance_pc_market_data: ResonancePcMarketDataService | None = None,
) -> List[Dict[str, Any]]:
    return _require_service(resonance_pc_market_data).query_products(
        scope=scope,
        city_id=city_id,
        side=side,
    )
