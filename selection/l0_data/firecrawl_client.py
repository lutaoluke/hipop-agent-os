"""
Firecrawl wrapper — 包重试 / 429 退避 / credits 计数日志.

§4.2 关键工程要点:
- noon 用默认 basic proxy (1 cr/页)
- Amazon UAE/SA 必须 proxy='stealth' (5 cr/页), 否则 503
- waitFor 3-5s 给 JS 渲染
- 1688 不走 Firecrawl (§4.1: 出口 IP 在美国, 1688 风控易识别 session 异常)

env: FIRECRAWL_API_KEY (selector/.env)
"""
from __future__ import annotations
import os, sys, time, logging
from typing import Optional


log = logging.getLogger("firecrawl_client")


class FirecrawlScrapeError(Exception):
    pass


_app = None


def _client():
    """延后 init, 避免没装 SDK 时也能 import 本模块."""
    global _app
    if _app is None:
        # 优先从 selection/.env 读
        env_path = os.path.normpath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", ".env"))
        if os.path.exists(env_path):
            try:
                from dotenv import load_dotenv
                load_dotenv(env_path)
            except ImportError:
                pass
        api_key = os.environ.get("FIRECRAWL_API_KEY")
        if not api_key:
            raise FirecrawlScrapeError(
                "FIRECRAWL_API_KEY 未设置 (检查 selection/.env 或环境变量)"
            )
        from firecrawl import FirecrawlApp
        _app = FirecrawlApp(api_key=api_key)
    return _app


# 实证成本 (§4.2)
PROXY_CREDIT_COST = {
    "basic": 1,
    "stealth": 5,
    "auto": 1,    # 默认认为 basic, 实际可能升级
}


def scrape(
    url: str,
    *,
    proxy: str = "basic",
    wait_for: int = 3000,
    formats: Optional[list[str]] = None,
    max_retries: int = 3,
    retry_base_seconds: float = 5.0,
    only_main_content: Optional[bool] = None,
) -> dict:
    """
    抓单页, 返回 {markdown, html?, metadata, credits_used}.

    重试策略:
    - 429 (rate limit): 指数退避 (5/10/20s)
    - InternalServerError (Firecrawl proxy 故障, 已实证 amazon stealth 偶发): 退避 + 1 次重试
    - 其他异常: 直接 raise
    """
    formats = formats or ["markdown"]
    fc = _client()
    last_exc = None

    for attempt in range(max_retries):
        try:
            r = fc.scrape(
                url,
                formats=formats,
                wait_for=wait_for,
                proxy=proxy,
                only_main_content=only_main_content,
            )
            md = (r.markdown or "") if hasattr(r, "markdown") else ""
            html = (r.html or "") if hasattr(r, "html") else ""
            metadata = {}
            if hasattr(r, "metadata") and r.metadata:
                # metadata 是 pydantic model in v2, 转 dict
                try:
                    metadata = r.metadata.model_dump() if hasattr(r.metadata, "model_dump") else dict(r.metadata)
                except Exception:
                    metadata = {"_raw": str(r.metadata)[:500]}

            credits = PROXY_CREDIT_COST.get(proxy, 1)
            log.info("[firecrawl] OK %s proxy=%s md_len=%d credits=%d",
                     url[:80], proxy, len(md), credits)
            return {
                "success": True, "url": url, "proxy": proxy,
                "markdown": md, "html": html,
                "metadata": metadata, "credits_used": credits,
            }
        except Exception as e:
            last_exc = e
            etype = type(e).__name__
            msg = str(e)[:300]
            # 429 / proxy-tunnel-fail / internal 都退避重试
            transient = (
                "429" in msg or "rate" in msg.lower()
                or "tunnel" in msg.lower() or "InternalServerError" in etype
                or "ProxyError" in etype
            )
            if attempt < max_retries - 1 and transient:
                wait = retry_base_seconds * (2 ** attempt)
                log.warning("[firecrawl] %s transient (%s), 退避 %.1fs (attempt %d/%d)",
                            url[:60], etype, wait, attempt + 1, max_retries)
                time.sleep(wait)
                continue
            log.error("[firecrawl] FAIL %s: %s: %s", url[:80], etype, msg)
            break

    raise FirecrawlScrapeError(
        f"scrape 失败 after {max_retries} attempts: {url}: {last_exc}"
    )


def get_credit_usage() -> dict:
    """看当前账号余额 / 预算用."""
    cu = _client().get_credit_usage()
    # cu 是 pydantic model
    if hasattr(cu, "model_dump"):
        return cu.model_dump()
    return {"remaining_credits": getattr(cu, "remaining_credits", None),
            "plan_credits": getattr(cu, "plan_credits", None)}


if __name__ == "__main__":
    import json
    print(json.dumps(get_credit_usage(), default=str, indent=2))
