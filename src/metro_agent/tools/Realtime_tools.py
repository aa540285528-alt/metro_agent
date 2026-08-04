import os

import requests
from langchain_core.tools import tool
from metro_agent.config import WIREMOCK_BASE_URL


def _alarm_base_url() -> str:
    return os.environ.get("AGENT_EVAL_ALARM_BASE_URL", WIREMOCK_BASE_URL).rstrip("/")


def normalize_system(system: str) -> str:
    system = (system or "").strip()

    if "骨干" in system and "传输" in system:
        return "骨干传输"
    if "传输" in system:
        return "骨干传输"
    if "无线" in system:
        return "无线"
    if "集中告警" in system:
        return "集中告警"
    if "电源" in system:
        return "电源"

    return system


@tool
def query_alarm_tool(
        line:str,
        station:str,
        system:str
)->dict:
    """查询相关路线、站点、系统下的告警"""
    # 将传入的参数去除首尾空格（strip()）后，组装成字典，作为 HTTP 请求的查询参数（Query Parameters）
    query = {
        "line": line.strip(),
        "station": station.strip(),
        "system": normalize_system(system)
    }
    # 向 {WIREMOCK_BASE_URL}/alarm 发起 GET 请求，附带刚才组装的 query 参数，并设置 timeout=5（5秒超时），即如果服务端5秒内未响应将抛出超时异常。
    try:
        response = requests.get(
        f"{_alarm_base_url()}/api/alarms", 
        params=query,
        timeout=5
    )
        # 响应码非 2xx 时，抛出异常
        response.raise_for_status()
        alarms = response.json()
        if not isinstance(alarms, list):
            return{
                "success": False,
                "query":query,
                "error":"告警接口返回数据格式错误"
            }
        return {
            "success": True,
            "query": query,
            "count": len(alarms),
            "alarms": alarms
        }
    except requests.Timeout as e:
        return{
            "success": False,
            "query":query,
            "error":f"告警接口请求超时:{e}"
        }
    except (requests.RequestException,ValueError) as e:
        status_code = getattr(getattr(e, "response", None), "status_code", None)
        url = getattr(getattr(e, "response", None), "url", None)
        return{
            "success": False,
            "query":query,
            "status_code": status_code,
            "url": url,
            "error":f"告警接口请求异常:{e}"
        }
    
def query_all_alarm_tool():
    pass
