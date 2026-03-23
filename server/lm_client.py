"""
调用本地 LM Studio（OpenAI 兼容接口）对页面 title 做 SEO 分析。
默认 base_url: http://127.0.0.1:1234，可通过环境变量 LM_STUDIO_BASE_URL 覆盖。
"""

import json
import os
import re
import requests

LM_STUDIO_BASE_URL = os.environ.get("LM_STUDIO_BASE_URL", "http://127.0.0.1:1234").rstrip("/")
DEFAULT_TIMEOUT = 60


def _get_model_id():
    """获取当前已加载的模型 id（LM Studio 使用 /v1/models）。"""
    try:
        r = requests.get(f"{LM_STUDIO_BASE_URL}/v1/models", timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        models = data.get("data") or []
        if models:
            return models[0].get("id")
    except Exception:
        pass
    return None


def analyze_seo_title(title: str, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """
    对单个页面标题做 SEO 评分、改进建议与 3 个优化版标题。
    返回 dict: score, suggestions, alternatives (list[str], 3 个改写版本), error, raw。
    """
    model = _get_model_id()
    if not model:
        return {
            "score": None,
            "suggestions": [],
            "alternatives": [],
            "error": "无法获取模型：请确认 LM Studio 已启动并已加载模型",
            "raw": None,
        }

    system = (
        "你是 SEO 专家。对给定的网页标题进行：1) SEO 质量评分（1-10 分）；2) 2～3 条简短改进建议；"
        "3) 直接给出 3 个优化后的标题写法（可直接用作新 title）。"
        "请仅用以下 JSON 格式回复，不要其他内容："
        '{"score": 数字, "suggestions": ["建议1", "建议2"], "alternatives": ["优化标题1", "优化标题2", "优化标题3"]}'
    )
    user = f"请分析以下网页标题的 SEO 表现，并给出评分、建议和 3 个优化版标题：\n\n{title}"

    try:
        r = requests.post(
            f"{LM_STUDIO_BASE_URL}/v1/chat/completions",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.3,
                "max_tokens": 800,
            },
            timeout=timeout,
        )
    except requests.exceptions.Timeout:
        return {"score": None, "suggestions": [], "alternatives": [], "error": "请求 LM Studio 超时", "raw": None}
    except requests.exceptions.ConnectionError:
        return {"score": None, "suggestions": [], "alternatives": [], "error": "无法连接 LM Studio，请确认服务已启动（如 http://127.0.0.1:1234）", "raw": None}
    except Exception as e:
        return {"score": None, "suggestions": [], "alternatives": [], "error": str(e), "raw": None}

    if r.status_code != 200:
        return {
            "score": None,
            "suggestions": [],
            "alternatives": [],
            "error": f"LM Studio 返回 {r.status_code}",
            "raw": r.text[:500] if r.text else None,
        }

    try:
        data = r.json()
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    except Exception:
        return {"score": None, "suggestions": [], "alternatives": [], "error": "解析 LM 响应失败", "raw": None}

    # 尝试从回复中解析 JSON（含 score, suggestions, alternatives）
    score, suggestions, alternatives = None, [], []
    stripped = content.strip()
    # 先尝试整段或 code block 内的 JSON
    for block in [stripped, re.sub(r"^```\w*\s*", "", re.sub(r"\s*```\s*$", "", stripped))]:
        try:
            obj = json.loads(block)
            score = obj.get("score")
            suggestions = obj.get("suggestions") or []
            alternatives = obj.get("alternatives") or []
            break
        except (json.JSONDecodeError, TypeError):
            m = re.search(r"\{[^{}]*\"score\"[^{}]*\"suggestions\"[^{}]*\"alternatives\"[^{}]*\}", block, re.DOTALL)
            if m:
                try:
                    obj = json.loads(m.group(0))
                    score = obj.get("score")
                    suggestions = obj.get("suggestions") or []
                    alternatives = obj.get("alternatives") or []
                    break
                except (json.JSONDecodeError, TypeError):
                    pass
    if score is None and not suggestions and not alternatives:
        score_m = re.search(r"[\"']?score[\"']?\s*[:=]\s*(\d+)", content, re.I)
        if score_m:
            score = int(score_m.group(1))
        sugg_m = re.findall(r"[\"']([^\"']{2,80})[\"']", content)
        if sugg_m:
            suggestions = sugg_m[:3]

    if score is not None and (score < 1 or score > 10):
        score = max(1, min(10, score))
    if not isinstance(alternatives, list):
        alternatives = []

    return {
        "score": score,
        "suggestions": suggestions if isinstance(suggestions, list) else [],
        "alternatives": alternatives[:3],
        "error": None,
        "raw": content[:300],
    }
