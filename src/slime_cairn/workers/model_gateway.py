"""Competition model-gateway route validation.

The gateway performs upstream forwarding. This guard prevents competition
Workers from silently bypassing it or declaring a non-whitelisted route.
"""

from __future__ import annotations

from typing import Mapping
from urllib.parse import urlparse


ALLOWED_COMPETITION_MODEL_ENDPOINTS = frozenset(
    {
        "https://api.deepseek.com/chat/completions",
        "https://api.deepseek.com/v1/chat/completions",
        "https://api.deepseek.com/responses",
        "https://api.deepseek.com/anthropic/v1/messages",
        "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        "https://dashscope.aliyuncs.com/compatible-mode/v1/responses",
        "https://dashscope.aliyuncs.com/apps/anthropic/v1/messages",
        "https://coding.dashscope.aliyuncs.com/v1/chat/completions",
        "https://coding.dashscope.aliyuncs.com/apps/anthropic/v1/messages",
        "https://qianfan.baidubce.com/v2/chat/completions",
        "https://qianfan.baidubce.com/v2/responses",
        "https://qianfan.baidubce.com/anthropic/v1/messages",
        "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
        "https://ark.cn-beijing.volces.com/api/v3/responses",
        "https://ark.cn-beijing.volces.com/api/compatible/v1/messages",
        "https://ark.cn-beijing.volces.com/api/coding/v3/chat/completions",
        "https://ark.cn-beijing.volces.com/api/coding/v3/responses",
        "https://ark.cn-beijing.volces.com/api/coding/v1/messages",
        "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        "https://open.bigmodel.cn/api/v1/responses",
        "https://open.bigmodel.cn/api/anthropic/v1/messages",
        "https://open.bigmodel.cn/api/coding/paas/v4/chat/completions",
        "https://api.z.ai/api/coding/paas/v4/chat/completions",
        "https://api.hunyuan.cloud.tencent.com/v1/chat/completions",
        "https://tokenhub.tencentmaas.com/v1/chat/completions",
        "https://tokenhub.tencentmaas.com/v1/responses",
        "https://tokenhub.tencentmaas.com/v1/messages",
        "https://api.lkeap.cloud.tencent.com/v1/chat/completions",
        "https://api.lkeap.cloud.tencent.com/anthropic/v1/messages",
        "https://api.lkeap.cloud.tencent.com/v3/chat/completions",
        "https://api.lkeap.cloud.tencent.com/api/anthropic/v1/messages",
        "https://api.lkeap.cloud.tencent.com/coding/v3/chat/completions",
        "https://api.lkeap.cloud.tencent.com/coding/anthropic/v1/messages",
        "https://api.moonshot.cn/v1/chat/completions",
        "https://api.kimi.com/coding/v1/chat/completions",
        "https://api.kimi.com/coding/v1/messages",
        "https://api.siliconflow.cn/v1/chat/completions",
        "https://api.siliconflow.cn/v1/messages",
        "https://api.minimaxi.com/v1/chat/completions",
        "https://api.minimaxi.com/v1/responses",
        "https://api.minimaxi.com/anthropic/v1/messages",
        "https://api.xiaomimimo.com/v1/chat/completions",
        "https://api.xiaomimimo.com/v1/responses",
        "https://api.xiaomimimo.com/anthropic/v1/messages",
        "https://api.stepfun.com/v1/chat/completions",
        "https://api.stepfun.com/v1/responses",
        "https://api.stepfun.com/v1/messages",
        "https://spark-api-open.xf-yun.com/v1/chat/completions",
        "https://api.sensenova.cn/compatible-mode/v2/chat/completions",
        "https://api.baichuan-ai.com/v1/chat/completions",
    }
)

_UPSTREAM_ENV_BY_WORKER = {
    "codex-cli": "SLIME_CODEX_UPSTREAM_ENDPOINT",
    "pi-cli": "SLIME_PI_UPSTREAM_ENDPOINT",
    "claude-code": "SLIME_CLAUDE_UPSTREAM_ENDPOINT",
    "deepseek-harness": "SLIME_DEEPSEEK_UPSTREAM_ENDPOINT",
}
_REQUIRED_SUFFIX_BY_PROTOCOL = {
    "openai-responses": "/responses",
    "openai-chat-completions": "/chat/completions",
    "anthropic-messages": "/v1/messages",
}


def competition_gateway_full_endpoint(
    worker_type: str,
    base_url: str,
    protocol: str,
    environment: Mapping[str, str],
) -> bool:
    """Whether SDK path joining must preserve a complete gateway endpoint."""

    enabled = str(environment.get("SLIME_COMPETITION_GATEWAY_ONLY", "")).strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return False
    configured = str(
        environment.get(f"SLIME_{worker_type.split('-')[0].upper()}_GATEWAY_FULL_ENDPOINT")
        or environment.get("SLIME_GATEWAY_FULL_ENDPOINT")
        or ""
    ).strip().lower()
    if configured not in {"1", "true", "yes", "on"}:
        return False
    parsed = urlparse(str(base_url).strip())
    return (
        parsed.scheme == "https"
        and bool(parsed.netloc)
        and parsed.path.startswith("/llm-gateway/proxy/e/")
        and protocol in _REQUIRED_SUFFIX_BY_PROTOCOL
    )


def validate_competition_model_route(
    worker_type: str,
    base_url: str,
    protocol: str,
    environment: Mapping[str, str],
) -> None:
    """Validate a model route when the competition-only guard is enabled."""

    enabled = str(environment.get("SLIME_COMPETITION_GATEWAY_ONLY", "")).strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return

    parsed = urlparse(str(base_url).strip())
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or not parsed.path.startswith("/llm-gateway/proxy/e/")
    ):
        raise ValueError(
            "Competition model gateway requires an HTTPS /llm-gateway/proxy/e/<endpointCode> base URL"
        )

    env_name = _UPSTREAM_ENV_BY_WORKER[worker_type]
    upstream = str(
        environment.get(env_name) or environment.get("SLIME_MODEL_UPSTREAM_ENDPOINT") or ""
    ).strip().rstrip("/")
    if not upstream:
        raise ValueError(f"{env_name} is required when SLIME_COMPETITION_GATEWAY_ONLY=true")
    if upstream not in ALLOWED_COMPETITION_MODEL_ENDPOINTS:
        raise ValueError(f"{env_name} is not in the authorized competition endpoint whitelist")
    required_suffix = _REQUIRED_SUFFIX_BY_PROTOCOL[protocol]
    if not upstream.endswith(required_suffix):
        raise ValueError(
            f"{env_name} must end with {required_suffix} for the configured {protocol} worker"
        )
