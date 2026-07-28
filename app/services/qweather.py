"""和风天气服务模块.

提供和风天气 API 调用的封装，包含按官方规范使用 EdDSA 签名算法生成 JWT，
提供 GeoLookup 城市搜索和 Weather 实时天气查询服务，包含动态 Token 刷新与完整重试机制。
"""

import time
from typing import Any, Optional

import httpx
import jwt
from tenacity import retry, stop_after_attempt, wait_exponential

from app.core.config import settings
from app.core.logging import logger


class QWeatherService:
    """和风天气 API 服务类.

    负责依据官方规范基于 EdDSA 算法生成 JWT 鉴权凭证，
    支持 Token 本地缓存与过期自动刷新，并封装和风 GeoLookup 与 Weather 业务 API。
    """

    def __init__(self) -> None:
        """初始化 JWT 缓存状态."""
        self._cached_jwt: Optional[str] = None
        self._jwt_expires_at: float = 0.0

    def _format_private_key(self, raw_key: str) -> str:
        """格式化私钥字符串，确保包含标准的 PEM 头尾边界和换行符."""
        key = raw_key.strip()
        if not key.startswith("-----BEGIN PRIVATE KEY-----"):
            key = f"-----BEGIN PRIVATE KEY-----\n{key}\n-----END PRIVATE KEY-----"
        return key

    def generate_jwt(self) -> str:
        """根据和风天气官方规范使用 EdDSA 算法生成 JWT."""
        if not settings.QWEATHER_KEY_ID or not settings.QWEATHER_PROJECT_ID or not settings.QWEATHER_PRIVATE_KEY:
            raise ValueError("和风天气凭据未完整配置，请检查 QWEATHER_KEY_ID, QWEATHER_PROJECT_ID, QWEATHER_PRIVATE_KEY")

        now = int(time.time())
        payload = {
            "iat": now - 30,
            "exp": now + 82800,  # 23小时有效
            "sub": settings.QWEATHER_PROJECT_ID,
        }
        headers = {
            "kid": settings.QWEATHER_KEY_ID,
        }

        formatted_private_key = self._format_private_key(settings.QWEATHER_PRIVATE_KEY)

        try:
            encoded_jwt = jwt.encode(
                payload,
                formatted_private_key,
                algorithm="EdDSA",
                headers=headers,
            )
            logger.info("qweather_jwt_generated", key_id=settings.QWEATHER_KEY_ID, project_id=settings.QWEATHER_PROJECT_ID)
            return encoded_jwt
        except Exception as e:
            logger.exception("qweather_jwt_generation_failed", error=str(e))
            raise RuntimeError(f"和风天气 JWT 签名失败: {str(e)}") from e

    def get_valid_jwt(self) -> str:
        """获取当前有效的 JWT Token (优先复用缓存，过期前 5 分钟自动刷新)."""
        now = time.time()
        if not self._cached_jwt or now >= (self._jwt_expires_at - 300):
            self._cached_jwt = self.generate_jwt()
            self._jwt_expires_at = now + 82800
        return self._cached_jwt

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=5))
    async def lookup_city(self, location: str, adm: Optional[str] = None) -> dict[str, Any]:
        """城市搜索 API (GeoLookup).

        参数：
            location: 需要查询的城市/区县名称（如 "朝阳"、"杭州"）或坐标。
            adm: 城市所属的上级行政区划分划（如 "北京"），辅助精准重名匹配。

        返回：
            dict: 和风 API 返回的 GeoLookup 结果 JSON 字典。
        """
        token = self.get_valid_jwt()
        headers = {"Authorization": f"Bearer {token}"}
        url = "https://geoapi.qweather.com/v2/city/lookup"
        params: dict[str, Any] = {"location": location}
        if adm:
            params["adm"] = adm

        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.get(url, headers=headers, params=params)
            response.raise_for_status()
            data = response.json()
            logger.info("qweather_city_lookup_response", code=data.get("code"), location=location, adm=adm)
            return data

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=5))
    async def get_weather_now(self, location: str) -> dict[str, Any]:
        """实时天气 API.

        参数：
            location: LocationID（如 "101010100"）或以逗号分隔的经纬度坐标。

        返回：
            dict: 和风 API 返回的 Weather Now 结果 JSON 字典。
        """
        token = self.get_valid_jwt()
        headers = {"Authorization": f"Bearer {token}"}
        url = "https://devapi.qweather.com/v7/weather/now"
        params = {"location": location}

        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.get(url, headers=headers, params=params)
            response.raise_for_status()
            data = response.json()
            logger.info("qweather_weather_now_response", code=data.get("code"), location=location)
            return data

    async def verify_jwt_and_fetch_weather(self, location: str = "101010100") -> dict[str, Any]:
        """通过发起真实 API 请求检验生成的 JWT Token 是否合法有效."""
        return await self.get_weather_now(location)


# 全局单例服务
qweather_service = QWeatherService()
