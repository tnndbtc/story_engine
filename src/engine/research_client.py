"""
research_client.py — HTTP client for the research_engine enrichment service.

Calls POST http://localhost:8010/enrich with topic + title + source_urls,
returns a knowledge_pack dict on success, or None on any failure.

Failure is always non-fatal: caller proceeds with unenriched generation.
"""

import logging
import os

import httpx

logger = logging.getLogger(__name__)

RESEARCH_ENGINE_URL = os.environ.get(
    "RESEARCH_ENGINE_URL", "http://localhost:8010/enrich"
)
TIMEOUT_SECONDS = 300  # 5 minutes — research engine can take 2–4 min


def enrich(
    topic: str,
    title: str,
    source_urls: list[str],
    lang_hint: str | None = None,
) -> dict | None:
    """
    Call research_engine and return a knowledge_pack dict, or None on failure.

    Args:
        topic:       Short topic string (e.g. canonical cluster title).
        title:       Original headline from the representative article.
        source_urls: List of URLs from cluster members — seeds retrieval.
        lang_hint:   Optional ISO 639-1 code (e.g. "zh"). Forwarded to the
                     research engine's language detection so non-English stories
                     use the correct retrieval locale without relying on
                     langdetect (which may not be installed).

    Returns:
        knowledge_pack dict on success, None on timeout / error / unavailable.
    """
    payload = {
        "topic":       topic,
        "title":       title,
        "source_urls": source_urls,
    }
    if lang_hint:
        payload["lang_hint"] = lang_hint
    try:
        logger.info(
            "research_client: calling enrich for topic=%r (%d source URLs)",
            topic[:80], len(source_urls),
        )
        resp = httpx.post(RESEARCH_ENGINE_URL, json=payload, timeout=TIMEOUT_SECONDS)
        resp.raise_for_status()
        kp = resp.json()
        confidence = kp.get("confidence_overall", "?")
        logger.info(
            "research_client: knowledge_pack received (confidence=%s, "
            "facts=%d, context=%d)",
            confidence,
            len(kp.get("fact_blocks", [])),
            len(kp.get("context_blocks", [])),
        )
        return kp
    except httpx.TimeoutException:
        logger.warning(
            "research_client: timeout after %ds — proceeding unenriched",
            TIMEOUT_SECONDS,
        )
    except httpx.HTTPStatusError as e:
        logger.warning(
            "research_client: HTTP %d from research_engine — proceeding unenriched",
            e.response.status_code,
        )
    except Exception as e:
        logger.warning(
            "research_client: unavailable (%s) — proceeding unenriched", e
        )
    return None
