from datetime import datetime, timedelta, timezone
import io
import json
from threading import Thread
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener

import pytest

from packages.resonance_shared import market_mirror as wire
from plans.resonance_pc.src.services import resonance_pc_market_data_service as market
from scripts.release import market_mirror as server


PAYLOAD = {"data": {"1": {"s": {"1": 100}}}}


@pytest.mark.parametrize("mode,official_works,mirror_works,sources,result_source", [
    ("automatic", True, True, ["columba"], "refresh"),
    ("automatic", False, True, ["columba", "mirror"], "mirror_fallback"),
    ("columba", False, True, ["columba"], None),
    ("mirror", True, False, ["mirror"], None),
    ("mirror", True, True, ["mirror"], "mirror"),
])
def test_source_modes_and_manual_proxy(tmp_path, monkeypatch, mode, official_works, mirror_works, sources, result_source):
    calls = []
    fetched_at = datetime.now(timezone.utc) - timedelta(seconds=30)
    service = market.ResonancePcMarketDataService(plan_root=tmp_path)
    config = tmp_path / "mirror.json"

    def official(*, proxy_port, timeout_seconds=None):
        assert proxy_port == 7890
        if mode == "automatic":
            assert timeout_seconds == service.AUTOMATIC_UPSTREAM_SECONDS
        calls.append("columba")
        if not official_works:
            raise market.ResonancePcMarketDataError("market_fetch_failed", "科伦巴连接失败。")
        return PAYLOAD

    def mirror(actual_config, *, proxy_port, timeout):
        assert actual_config == config and proxy_port == 7890 and timeout == service.MIRROR_TIMEOUT_SECONDS
        calls.append("mirror")
        if not mirror_works:
            raise wire.MarketMirrorError("蜜瓜转接不可用。")
        return wire.MarketMirrorSnapshot(fetched_at, PAYLOAD)

    monkeypatch.setattr(service, "fetch_raw", official)
    monkeypatch.setattr(service, "load_buy_lot_payload", lambda: {"city_product_buy_lot": {}})
    monkeypatch.setattr(market, "mirror_config_path", lambda: config)
    monkeypatch.setattr(market, "fetch_market_mirror", mirror)
    inputs = {"market_source_mode": mode, "market_proxy_mode": "manual", "market_proxy_port": 7890}
    if result_source is None:
        with pytest.raises(market.ResonancePcMarketDataError) as caught:
            service.refresh(**inputs)
        assert caught.value.code == "market_refresh_failed_no_cache"
    else:
        result = service.refresh(**inputs)
        assert result["market_source"] == result_source
        assert not result["stale"]
        if result_source.startswith("mirror"):
            assert result["fetched_at"] == service.load_latest()["fetched_at"] == fetched_at.isoformat()
    assert calls == sources


def test_missing_mirror_in_fixed_mode_never_tries_columba(tmp_path, monkeypatch):
    service = market.ResonancePcMarketDataService(plan_root=tmp_path)
    monkeypatch.setattr(service, "fetch_raw", lambda **_: pytest.fail("fixed mirror mode tried Columba"))
    monkeypatch.setattr(market, "mirror_config_path", lambda: None)
    with pytest.raises(market.ResonancePcMarketDataError, match="未配置蜜瓜转接"):
        service.refresh(market_source_mode="mirror")


def test_expired_mirror_contract():
    expired = datetime.now(timezone.utc) - timedelta(minutes=6)
    with pytest.raises(wire.MarketMirrorError, match="已过期"):
        wire.MarketMirrorSnapshot.parse(wire.MarketMirrorSnapshot(expired, PAYLOAD).to_dict())


def test_sync_preserves_previous_valid_snapshot(tmp_path, monkeypatch):
    path = tmp_path / "prices.json"
    response = PAYLOAD

    def open_upstream(*_args, **_kwargs):
        return io.BytesIO(json.dumps(response).encode())

    monkeypatch.setattr(server.request, "build_opener", lambda *_: SimpleNamespace(open=open_upstream))
    server.sync_snapshot(path)
    original = path.read_bytes()
    assert wire.MarketMirrorSnapshot.parse(json.loads(original)).payload == PAYLOAD
    response = {"data": {}}
    with pytest.raises(wire.MarketMirrorError):
        server.sync_snapshot(path)
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


def test_mirror_http_refuses_a_missing_or_expired_snapshot(tmp_path):
    with server.ThreadingHTTPServer(("127.0.0.1", 0), server.MarketHandler) as httpd:
        httpd.snapshot_path = tmp_path / "prices.json"
        worker = Thread(target=httpd.serve_forever, daemon=True)
        worker.start()
        opener = build_opener(ProxyHandler({}))
        url = f"http://127.0.0.1:{httpd.server_port}{wire.MARKET_PATH}"
        try:
            with pytest.raises(HTTPError) as missing:
                opener.open(url, timeout=2)
            assert missing.value.code == 503
            missing.value.close()
            fresh = wire.MarketMirrorSnapshot(datetime.now(timezone.utc), PAYLOAD)
            httpd.snapshot_path.write_bytes(server.encode_json(fresh.to_dict()))
            with opener.open(url, timeout=2) as response:
                assert wire.MarketMirrorSnapshot.parse(json.load(response)).payload == PAYLOAD
            expired = wire.MarketMirrorSnapshot(fresh.fetched_at - timedelta(minutes=6), PAYLOAD)
            httpd.snapshot_path.write_bytes(server.encode_json(expired.to_dict()))
            with pytest.raises(HTTPError) as stale:
                opener.open(url, timeout=2)
            assert stale.value.code == 503
            stale.value.close()
        finally:
            httpd.shutdown()
            worker.join()


def test_no_proxy_does_not_retry_through_windows_proxy(tmp_path, monkeypatch):
    service = market.ResonancePcMarketDataService(plan_root=tmp_path)
    monkeypatch.setattr(market, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(market.urllib.request, "getproxies", lambda: {"no": "*"})
    monkeypatch.setattr(market.urllib.request, "proxy_bypass", lambda _: True)
    monkeypatch.setattr(market, "get_market_url", lambda *args, **kwargs: pytest.fail("used Windows proxy"))

    def open_upstream(*_args, **_kwargs):
        raise URLError(TimeoutError("offline"))

    monkeypatch.setattr(market.urllib.request, "build_opener", lambda *_: SimpleNamespace(open=open_upstream))
    with pytest.raises(market.ResonancePcMarketDataError, match="无法连接科伦巴"):
        service.fetch_raw()
