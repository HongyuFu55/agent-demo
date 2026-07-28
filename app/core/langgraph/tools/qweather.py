"""和风天气 Agent Tool 定义."""

from typing import Optional
from langchain_core.tools import tool
from tenacity import retry, stop_after_attempt, wait_exponential

from app.core.logging import logger
from app.services.qweather import qweather_service


@tool
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=5))
async def search_location_id(location_name: str, province_or_city: Optional[str] = None) -> str:
    """查询城市或区县的 LocationID（查天气前如果不知道 ID 需先调用此工具）.

    参数：
        location_name: 区县或城市名称，例如 "朝阳"、"海淀"、"杭州"、"南山区"
        province_or_city: 上级省份或城市（可选），用于重名区分，例如 "北京"、"深圳"
    """
    try:
        data = await qweather_service.lookup_city(location=location_name, adm=province_or_city)
        if not isinstance(data, dict):
            return f"查询城市 ID 异常：和风 API 返回格式非字典结构 ({type(data).__name__})"

        code = str(data.get("code", ""))
        if code != "200":
            logger.warning("qweather_lookup_city_code_error", code=code, location=location_name)
            return f"未找到位置 '{location_name}'，API 返回错误码：{code}"

        locations = data.get("location")
        if not locations or not isinstance(locations, list):
            return f"未找到名称为 '{location_name}' 的城市或区县信息。"

        results = []
        for item in locations[:5]:
            if not isinstance(item, dict):
                continue
            name = item.get("name", "")
            loc_id = item.get("id", "")
            adm1 = item.get("adm1", "")
            adm2 = item.get("adm2", "")
            country = item.get("country", "")
            results.append(
                f"- 名称: {name} | LocationID: {loc_id} | 行政区划: {country} {adm1} {adm2}"
            )

        if not results:
            return f"未解析到有效的城市信息（查询词：'{location_name}'）。"

        return "找到匹配的城市/区县信息（请使用对应的 LocationID 查询天气）：\n" + "\n".join(results)
    except Exception as e:
        logger.exception("qweather_lookup_failed", location_name=location_name, error=str(e))
        return f"查询城市 ID 异常: {str(e)}"


@tool
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=5))
async def get_weather_now(location: str) -> str:
    """获取实时天气.

    参数：
        location: 可以是 search_location_id 返回的 LocationID（推荐，如 "101010100"），
                  或者格式为 "经度,纬度" 的坐标字符串（最多保留两位小数，如 "116.40,39.90"）。
    """
    try:
        # 保护逻辑：如果传入的是经纬度坐标，自动截断为 2 位小数，防止和风接口 400 报错
        if "," in location:
            parts = location.split(",")
            if len(parts) == 2:
                try:
                    lon = f"{float(parts[0].strip()):.2f}"
                    lat = f"{float(parts[1].strip()):.2f}"
                    location = f"{lon},{lat}"
                except ValueError:
                    pass

        data = await qweather_service.get_weather_now(location=location)

        # 1. 结构防御：校验响应格式
        if not isinstance(data, dict):
            return f"查询天气失败：和风 API 返回格式非字典结构 ({type(data).__name__})"

        code = str(data.get("code", ""))
        if code != "200":
            logger.warning("qweather_get_weather_code_error", code=code, location=location)
            return f"查询天气失败，和风 API 返回错误码：{code}"

        # 2. 字段防御：检查 now 节点及其内部具体字段是否存在
        now = data.get("now")
        if not now or not isinstance(now, dict):
            logger.warning("qweather_missing_now_field", data=data, location=location)
            return "查询天气失败：和风 API 返回结果中缺少实时天气数据 ('now' 节点不存在或非字典)。"

        weather_text = now.get("text", "未知")
        temp = now.get("temp", "N/A")
        feels_like = now.get("feelsLike", "N/A")
        wind_dir = now.get("windDir", "未知")
        wind_scale = now.get("windScale", "N/A")
        humidity = now.get("humidity", "N/A")
        precip = now.get("precip", "N/A")

        return (
            f"【实时天气情况】\n"
            f"天气状况：{weather_text}\n"
            f"当前气温：{temp}℃ (体感 {feels_like}℃)\n"
            f"风向风力：{wind_dir} {wind_scale}级\n"
            f"相对湿度：{humidity}%\n"
            f"降水量：{precip}mm"
        )
    except Exception as e:
        logger.exception("qweather_weather_failed", location=location, error=str(e))
        return f"查询天气发生异常: {str(e)}"
