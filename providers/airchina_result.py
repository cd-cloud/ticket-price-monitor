"""Diagnostic result assembly, separate from production ProviderQueryOutcome.

The production contract requires a final `price`. Do not coerce starting/base
fares into that contract until taxes and complete itinerary evidence exist.
"""

MESSAGES = {
    "reference_fares_observed": "已读取稳定的航班舱位不含税起价；不是含税总价，未保存报价。",
    "fare_products_observed": "已读取指定航班的具体票价产品及不含税基础票价；未确认税费或可出票性，未保存报价。",
    "needs_verification": "国航要求人工验证；停止读取，不自动重试或切换后端。",
    "needs_notice_confirmation": "乘机提示需要用户自行确认；未自动确认。",
    "blocked": "访问被限制；停止自动请求。",
    "stale_results": "官网提示结果已过期；已丢弃本次预览，不自动刷新。",
    "unexpected_page": "页面已离开预期航线；已丢弃本次预览。",
    "invalid_snapshot": "未取得可校验的页面结构。",
    "query_page_unverified": "等待结束，未取得稳定且可校验的结果；不能据此判断无票。",
    "product_details_unverified": "未取得稳定且可校验的指定产品；已丢弃本次预览。",
}


def build_preview_result(wait_result, products=None):
    """Only derived parser fields escape; failed expansion invalidates previews."""
    result = dict(wait_result)
    if products is not None:
        if products["status"] == "fare_products_observed" and products.get("products"):
            result.update(state="fare_products_observed", product_preview=products)
        else:
            reason = products.get("reason", "product_details_unverified")
            result["state"] = reason if reason in MESSAGES else "product_details_unverified"
            result["reference_preview"] = {
                "status": "unverified", "reason": reason, "offers": [],
                "quote_saved": False, "parser_ready": False, "coverage_complete": False,
            }
            result["product_preview"] = {**products, "products": []}
    result.update(
        message=MESSAGES[result["state"]], parser_ready=False, quote_saved=False,
        coverage_complete=False, final_quote_supported=False,
    )
    return result
