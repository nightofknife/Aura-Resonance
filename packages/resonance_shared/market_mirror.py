"""Wire format and private TLS client for the market mirror."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import http.client
import json
import os
from pathlib import Path
import ssl
import sys
from urllib import error, parse, request


MARKET_PATH = "/market/v1/prices"
MARKET_UPSTREAM = "https://www.resonance-columba.com/api/get-prices"
MAX_MARKET_BYTES = 2 * 1024 * 1024
MAX_MIRROR_AGE = timedelta(minutes=5)
MAX_CLOCK_SKEW = timedelta(seconds=30)
MIRROR_RESOURCE = "aura-market-mirror.json"
MIRROR_CONFIG_ENV = "AURA_MARKET_MIRROR_CONFIG"


class MarketMirrorError(RuntimeError):
    """A mirror failure suitable for diagnostics without exposing its address."""


class RejectRedirects(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class LocalMarketProxyHandler(request.ProxyHandler):
    def __init__(self, port: int):
        address = f"127.0.0.1:{port}"
        super().__init__({"http": address, "https": address})

    def proxy_open(self, req, proxy, scheme):
        req.set_proxy(proxy, "http")
        return None


def validate_market_payload(payload: object) -> dict:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or not data:
        raise MarketMirrorError("行情数据缺少商品价格。")
    has_prices = False
    for product in data.values():
        if not isinstance(product, dict):
            raise MarketMirrorError("商品价格结构无效。")
        for side in ("b", "s"):
            prices = product.get(side)
            if prices is not None and not isinstance(prices, dict):
                raise MarketMirrorError("商品城市价格结构无效。")
            has_prices |= bool(prices)
    if not has_prices:
        raise MarketMirrorError("行情数据没有可用的买卖价格。")
    return payload


def read_market_json(stream) -> object:
    raw = stream.read(MAX_MARKET_BYTES + 1)
    if len(raw) > MAX_MARKET_BYTES:
        raise MarketMirrorError("行情响应超过大小限制。")
    try:
        return json.loads(raw)
    except (UnicodeError, ValueError):
        raise MarketMirrorError("行情响应不是有效的 JSON。") from None


@dataclass(frozen=True)
class MarketMirrorSnapshot:
    fetched_at: datetime
    payload: dict

    def to_dict(self) -> dict:
        return {"version": 1, "fetched_at": self.fetched_at.isoformat(), "payload": self.payload}

    @classmethod
    def parse(cls, value: object) -> MarketMirrorSnapshot:
        if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1:
            raise MarketMirrorError("蜜瓜转接行情格式无效。")
        try:
            fetched_at = datetime.fromisoformat(value["fetched_at"])
            if fetched_at.utcoffset() is None:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise MarketMirrorError("蜜瓜转接行情时间无效。") from None
        age = datetime.now(timezone.utc) - fetched_at
        if age > MAX_MIRROR_AGE:
            raise MarketMirrorError("蜜瓜转接行情已过期。")
        if age < -MAX_CLOCK_SKEW:
            raise MarketMirrorError("蜜瓜转接行情时间超出当前时间。")
        return cls(fetched_at.astimezone(timezone.utc), validate_market_payload(value.get("payload")))


def mirror_config_path() -> Path | None:
    override = os.environ.get(MIRROR_CONFIG_ENV)
    if override:
        return Path(override)
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        resource = Path(bundle) / MIRROR_RESOURCE
        if resource.is_file():
            return resource
    return None


def fetch_market_mirror(config: Path | None, *, proxy_port: int | None, timeout: float) -> MarketMirrorSnapshot:
    if config is None:
        raise MarketMirrorError("当前安装未配置蜜瓜转接。")
    try:
        settings = json.loads(config.read_text(encoding="utf-8-sig"))
        address = settings["url"]
        if not isinstance(address, str):
            raise ValueError
        url = parse.urlsplit(address)
        port = url.port
        if (url.scheme != "https" or not url.hostname or port == 0 or url.username is not None
                or url.path not in ("", "/") or url.query or url.fragment):
            raise ValueError
        context = ssl.create_default_context(cadata=settings["ca_pem"])
    except (OSError, ValueError, KeyError, TypeError):
        raise MarketMirrorError("蜜瓜转接地址或专用 CA 配置无效。") from None
    proxy = request.ProxyHandler({}) if proxy_port is None else LocalMarketProxyHandler(proxy_port)
    opener = request.build_opener(proxy, RejectRedirects(), request.HTTPSHandler(context=context))
    req = request.Request(url._replace(path=MARKET_PATH).geturl(), headers={"User-Agent": "AuraMarketMirror/1"})
    try:
        with opener.open(req, timeout=timeout) as response:
            return MarketMirrorSnapshot.parse(read_market_json(response))
    except error.HTTPError as exc:
        status = exc.code
        exc.close()
        if status == 503:
            raise MarketMirrorError("蜜瓜转接暂无有效行情，可能仍在同步或已过期。") from None
        raise MarketMirrorError(f"蜜瓜转接返回 HTTP {status}。") from None
    except (OSError, http.client.HTTPException) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, ssl.SSLCertVerificationError):
            raise MarketMirrorError("蜜瓜转接 HTTPS 证书校验失败。") from None
        raise MarketMirrorError(f"无法连接蜜瓜转接（{type(reason).__name__}）。") from None
