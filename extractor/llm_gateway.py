# Copyright (c) 2026 AetherOmni Contributors.
#
# This file is part of AetherOmni.
#
# AetherOmni is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# AetherOmni is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with AetherOmni.  If not, see <https://www.gnu.org/licenses/>.


from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

MODEL_GEMINI_FLASH_LITE = "gemini-2.5-flash-lite"
MODEL_GEMINI_FLASH = "gemini-2.5-flash"
MODEL_OPENROUTER_FREE = "openrouter/free"
_NOT_FOUND = "not found"
PREFIX_GOOGLE = "google/"

# Shared Constants to avoid SonarQube Duplication smell
APPLICATION_JSON = "application/json"
KNATIVE_MIN_SCALE = "autoscaling.knative.dev/minScale"
PROCESS_DOCUMENT_TASK = "extractor.tasks.process_document_task"

# Google GenAI imports
try:
    from google import genai
    from google.genai import types
    from google.genai.errors import APIError
except ImportError:

    class APIError(Exception):  # type: ignore[no-redef]
        pass


GEMINI_API_KEY_ERROR = "GEMINI_API_KEY is not configured."
VAL_ERROR_UNAVAILABLE = "GEMINI_API_KEY is not configured and Vertex AI is unavailable."
# Note: MODEL_GEMINI_FLASH is defined at module top (line ~37); not redefined here.
MIME_PDF = "application/pdf"
MIME_OCTET_STREAM = "application/octet-stream"


@dataclass
class UnifiedResponse:
    """Unified response wrapper returned by all LLM gateway functions."""

    text: str
    in_toks: int
    out_toks: int
    cost_val: Decimal
    model_used: str

    @property
    def input_tokens(self) -> int:
        return self.in_toks

    @property
    def output_tokens(self) -> int:
        return self.out_toks

    @property
    def cost_usd(self) -> Decimal:
        return self.cost_val


class BudgetExceededException(Exception):
    """Raised when cumulative extraction costs for the month exceed the set budget."""


class GeminiProcessingError(Exception):
    """Raised when the Gemini API processing or upload fails."""


def check_budget_and_api_limit() -> None:
    """
    Sums the cost of all source documents processed in the current calendar month
    (including documents already deleted, via MonthlySpendLog) and raises an
    exception if the budget limit is exceeded.
    """
    from django.conf import settings
    from django.db import models

    from extractor.models import MonthlySpendLog, SourceDocument, SystemSettings

    now = timezone.now()
    first_of_month = datetime(now.year, now.month, 1, tzinfo=UTC)

    # EDGE-03 / Q2 fix: Deleted documents (flushed to log before removal)
    logged_spent = MonthlySpendLog.total_for_month(now.year, now.month)

    if getattr(settings, "SURREALDB_OFFLINE", False):
        live_spent = SourceDocument.objects.filter(created_at__gte=first_of_month).aggregate(
            total=models.Sum("cost_usd")
        )["total"] or Decimal("0.0")
        settings_obj = SystemSettings.get_settings()
        monthly_cap = Decimal(str(settings_obj.monthly_budget_usd))
    else:
        from extractor import surreal_db
        from extractor.tasks import format_datetime

        # EDGE-03 / Q2 decision: SurrealDB BEGIN TRANSACTION block for atomic budget evaluation
        transaction_sql = """
        BEGIN TRANSACTION;
        LET $live = (SELECT math::sum(cost_usd) AS total FROM documents WHERE created_at >= <datetime> $first_of_month GROUP ALL);
        LET $spent = IF array::len($live) > 0 THEN $live[0].total ELSE 0.0 END;
        LET $settings = (SELECT monthly_budget_usd FROM system_settings:global);
        LET $cap = IF array::len($settings) > 0 THEN $settings[0].monthly_budget_usd ELSE 10.0 END;
        RETURN { live_spent: $spent, cap: $cap };
        COMMIT TRANSACTION;
        """
        res = surreal_db._first_result(
            surreal_db._run(transaction_sql, {"first_of_month": format_datetime(first_of_month)})
        )
        if res and isinstance(res, dict):
            live_spent = Decimal(str(res.get("live_spent", 0.0)))
            monthly_cap = Decimal(str(res.get("cap", 10.0)))
        else:
            live_spent = Decimal("0.0")
            monthly_cap = Decimal("10.0")

    total_spent = live_spent + logged_spent

    if total_spent >= monthly_cap:
        raise BudgetExceededException(
            f"Monthly budget limit of ${monthly_cap:.2f} USD has been reached. Current usage: ${total_spent:.4f} USD."
        )


def _get_cached_realtime_pricing(cache) -> dict[str, dict[str, Decimal]] | None:
    """Helper to check SurrealDB or Django cache for realtime pricing."""
    cached_pricing = None
    try:
        from extractor import surreal_db

        cached_pricing = surreal_db.kv_cache_get("realtime_model_pricing")
    except (RuntimeError, ValueError, KeyError, AttributeError) as exc:
        logger.debug("[Pricing API] Failed to read cached pricing: %s", exc)

    if not cached_pricing:
        cached_pricing = cache.get("realtime_model_pricing")

    if cached_pricing:
        try:
            return {
                k: {
                    "prompt": Decimal(str(v["prompt"])),
                    "completion": Decimal(str(v["completion"])),
                }
                for k, v in cached_pricing.items()
            }
        except (KeyError, ValueError, TypeError, AttributeError) as e:
            logger.warning("[Pricing API] Error parsing cached pricing values: %s. Clearing cache.", e)

    return None


def _parse_openrouter_pricing_response(data: dict) -> dict[str, dict[str, str]]:
    """Helper to extract model prompt and completion rates from OpenRouter API payload."""
    pricing_map = {}
    for m in data.get("data", []):
        m_id = m.get("id", "").lower().strip()
        pricing = m.get("pricing", {})
        prompt_val = pricing.get("prompt", "0")
        completion_val = pricing.get("completion", "0")
        if m_id:
            pricing_map[m_id] = {
                "prompt": str(prompt_val),
                "completion": str(completion_val),
            }
    return pricing_map


def fetch_realtime_model_pricing() -> dict[str, dict[str, Decimal]] | None:
    """
    Fetches real-time model pricing from OpenRouter API and caches it for 24 hours.
    Returns a dictionary mapping lowercased model IDs/names to prompt/completion Decimal rates per token.
    """
    from django.core.cache import cache

    cached = _get_cached_realtime_pricing(cache)
    if cached:
        return cached

    # SEC-05 fix: Fetch live pricing using httpx with explicit SSL verification
    try:
        response = httpx.get("https://openrouter.ai/api/v1/models", timeout=5.0, verify=True)
        if response.status_code == 200:
            pricing_map = _parse_openrouter_pricing_response(response.json())
            try:
                from extractor import surreal_db

                surreal_db.kv_cache_set("realtime_model_pricing", pricing_map, ttl_seconds=86400)
            except (RuntimeError, ValueError, KeyError, AttributeError) as exc:
                logger.debug("[Pricing API] Failed to write cache: %s", exc)

            cache.set("realtime_model_pricing", pricing_map, 86400)
            return {
                k: {
                    "prompt": Decimal(v["prompt"]),
                    "completion": Decimal(v["completion"]),
                }
                for k, v in pricing_map.items()
            }
    except (httpx.HTTPError, ValueError, TypeError, KeyError, OSError) as exc:
        logger.warning("[Pricing API] Failed to fetch real-time model pricing: %s. Falling back.", exc)

    return None


def resolve_realtime_pricing(model_name: str) -> tuple[Decimal, Decimal] | None:
    """
    Looks up prompt and completion rates per token for the given model_name
    using the cached real-time pricing data.
    """
    try:
        pricing_map = fetch_realtime_model_pricing()
        if not pricing_map:
            return None

        model_name = model_name.lower().strip()

        # 1. Direct match
        if model_name in pricing_map:
            p = pricing_map[model_name]
            return p["prompt"], p["completion"]

        # 2. Match without provider prefix (e.g. "google/gemini-3.5-flash" -> "gemini-3.5-flash")
        for m_id, pricing in pricing_map.items():
            if m_id == model_name or m_id.split("/")[-1] == model_name:
                return pricing["prompt"], pricing["completion"]

        # 3. Soft match (e.g. "gemini-3.5-flash" in "google/gemini-3.1-flash-lite")
        for m_id, pricing in pricing_map.items():
            if model_name in m_id or m_id in model_name:
                return pricing["prompt"], pricing["completion"]
    except (KeyError, ValueError, TypeError, AttributeError) as e:
        logger.debug("[Pricing API] Error resolving pricing for %s: %s", model_name, e)

    return None


def calculate_gemini_cost(model_name: str, input_tokens: int, output_tokens: int) -> Decimal:
    model_name = model_name.lower().strip()
    input_tokens = int(input_tokens)
    output_tokens = int(output_tokens)

    rt_pricing = resolve_realtime_pricing(model_name)
    if rt_pricing:
        prompt_rate, completion_rate = rt_pricing
        return (Decimal(input_tokens) * prompt_rate) + (Decimal(output_tokens) * completion_rate)

    if "embedding" in model_name or "embed" in model_name:
        return Decimal("0.00")
    elif "lite" in model_name:
        in_rate = Decimal("0.25")
        out_rate = Decimal("1.50")
    else:
        # Safe default for modern 3.x general requests (including 3.5 flash)
        in_rate = Decimal("1.50")
        out_rate = Decimal("9.00")

    cost = (Decimal(input_tokens) / Decimal(1000000) * in_rate) + (Decimal(output_tokens) / Decimal(1000000) * out_rate)
    return cost


def calculate_openrouter_cost(model_name: str, input_tokens: int, output_tokens: int) -> Decimal:
    """
    Precise cost calculator for OpenRouter models. Returns $0.00 for :free models.
    Uses real-time rates resolved dynamically, falling back to static tiers if offline.
    """
    model_name = model_name.lower().strip()
    input_tokens = int(input_tokens)
    output_tokens = int(output_tokens)

    if ":free" in model_name or "free" in model_name:
        return Decimal("0.0")

    # Try resolving real-time pricing first
    rt_pricing = resolve_realtime_pricing(model_name)
    if rt_pricing:
        prompt_rate, completion_rate = rt_pricing
        return (Decimal(input_tokens) * prompt_rate) + (Decimal(output_tokens) * completion_rate)

    # Standard rates (e.g. general fallback $0.50 per M tokens)
    return (Decimal(input_tokens) / Decimal(1000000) * Decimal("0.50")) + (
        Decimal(output_tokens) / Decimal(1000000) * Decimal("1.50")
    )


def _get_openrouter_api_key() -> str:
    """SEC-02 fix: Resolve OPENROUTER_API_KEY strictly from environment variable / GCP Secret Manager.

    Database storage is disabled per Q1 DevSecOps policy to prevent CWE-312 plaintext credential leaks.
    """
    return os.getenv("OPENROUTER_API_KEY", "").strip()


def _call_openrouter(prompt: str, system_instruction: str | None, model_name: str) -> UnifiedResponse:
    """
    Helper function to dispatch completions requests to OpenRouter over HTTPS.
    """
    url = "https://openrouter.ai/api/v1/chat/completions"
    api_key = _get_openrouter_api_key()

    messages = []
    if system_instruction:
        messages.append({"role": "system", "content": system_instruction})
    messages.append({"role": "user", "content": prompt})

    # Use APP_URL from settings (no hardcoded localhost referer in production)
    app_url = str(getattr(settings, "APP_URL", None) or os.getenv("APP_URL", "http://localhost:8080"))
    headers: dict[str, str] = {
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": app_url,
        "X-Title": "Knowledge Desk",
        "Content-Type": APPLICATION_JSON,
    }

    payload = {"model": model_name, "messages": messages, "temperature": 0.2}

    # SEC-05 fix: Execute POST request using httpx with explicit SSL verification
    try:
        response = httpx.post(url, json=payload, headers=headers, timeout=60.0, verify=True)
        response.raise_for_status()
        result = response.json()

        choice = result["choices"][0]["message"]
        text_content = choice.get("content", "")

        # Parse token usage
        usage = result.get("usage", {})
        input_tokens = usage.get("prompt_tokens", 0)
        output_tokens = usage.get("completion_tokens", 0)

        cost = calculate_openrouter_cost(model_name, input_tokens, output_tokens)

        return UnifiedResponse(text_content, input_tokens, output_tokens, cost, model_name)

    except Exception as e:
        logger.exception("[Gateway Error] OpenRouter request failed for %s.", model_name)
        raise GeminiProcessingError(f"OpenRouter routing error: {e!s}")


def _try_vertex_fallback_chain(model_name, contents, config):
    for region in VERTEX_REGION_FALLBACK_CHAIN:
        vertex_client = get_vertex_client_for_location(region)
        if vertex_client:
            try:
                logger.info(
                    "[Gateway] Attempting generation on Vertex AI in %s using model %s...",
                    region,
                    model_name,
                )
                response = execute_with_backoff(
                    vertex_client.models.generate_content, model=model_name, contents=contents, config=config
                )
                input_tokens = response.usage_metadata.prompt_token_count or 0
                output_tokens = response.usage_metadata.candidates_token_count or 0
                cost = calculate_gemini_cost(model_name, input_tokens, output_tokens)
                return UnifiedResponse(response.text, input_tokens, output_tokens, cost, model_name)
            except Exception as e:
                logger.warning("[Gateway] Vertex AI generation in region %s failed: %s.", region, e)
    return None


def _try_ai_studio_fallback(model_name, contents, config):
    api_key = getattr(settings, "GEMINI_API_KEY", "")
    if api_key and api_key.strip().lower() in ["", "none", "your-gemini-api-key", "placeholder"]:
        api_key = None

    if api_key:
        try:
            logger.info("[Gateway] Attempting generation on AI Studio fallback using model %s...", model_name)
            client = genai.Client(api_key=api_key)
            response = execute_with_backoff(
                client.models.generate_content, model=model_name, contents=contents, config=config
            )
            input_tokens = response.usage_metadata.prompt_token_count or 0
            output_tokens = response.usage_metadata.candidates_token_count or 0
            cost = calculate_gemini_cost(model_name, input_tokens, output_tokens)
            return UnifiedResponse(response.text, input_tokens, output_tokens, cost, model_name)
        except Exception as e:
            logger.warning("[Gateway] AI Studio generation failed: %s.", e)
    return None


def _call_direct_gemini(
    prompt: str,
    system_instruction: str | None,
    model_name: str,
    files: list[Any] | None = None,
) -> UnifiedResponse:
    """
    Helper function to dispatch completions requests directly to Google Gemini API.
    Attempts Vertex AI across the VERTEX_REGION_FALLBACK_CHAIN and falls back to AI Studio if needed.
    """
    config = types.GenerateContentConfig(temperature=0.2)
    if system_instruction:
        config.system_instruction = system_instruction

    contents = []
    if files:
        contents.extend(files)
    contents.append(prompt)

    # 1. Try Vertex AI via ADC across regional fallback chain
    res = _try_vertex_fallback_chain(model_name, contents, config)
    if res:
        return res

    # 2. Fallback to AI Studio key
    res = _try_ai_studio_fallback(model_name, contents, config)
    if res:
        return res

    raise ValueError("Generation failed: both Vertex AI and AI Studio pathways are exhausted.")


# Allowlist of valid Gemini / Vertex model IDs (without google/ prefix).
# If a model name from the DB or config is not on this list and doesn't look
# like a qualified OpenRouter path, it will be replaced with the default to
# prevent 404 errors from stale or mis-typed configurations.
KNOWN_GEMINI_MODELS: frozenset[str] = frozenset(
    {
        MODEL_GEMINI_FLASH,
        MODEL_GEMINI_FLASH_LITE,
        "gemini-2.5-pro",
        "gemini-3.1-pro",
        "auto",
    }
)


def get_cheapest_regional_gemini_model(region: str | None = None, is_vision: bool = False) -> str:
    """
    Dynamically resolves the most cost-effective active Gemini model in the specified GCP region.
    Evaluates real-time token pricing and regional availability, caching the optimal model for 24 hours.
    """
    from django.core.cache import cache

    resolved_region = region or os.getenv("CLOUD_ML_REGION") or os.getenv("GOOGLE_CLOUD_REGION") or "asia-southeast1"
    cache_key = f"cheapest_gemini_model_{resolved_region}_{is_vision}"
    cached_model = cache.get(cache_key)
    if cached_model:
        return cached_model

    # Prioritized candidate models ordered by cost efficiency (cheapest first)
    if is_vision:
        candidates = [
            MODEL_GEMINI_FLASH,  # $0.075/1M - state of the art multimodal
            MODEL_GEMINI_FLASH_LITE,  # $0.038/1M
            "gemini-2.0-flash",
            "gemini-1.5-flash",
            "gemini-2.5-pro",
        ]
    else:
        candidates = [
            MODEL_GEMINI_FLASH_LITE,  # $0.038/1M
            MODEL_GEMINI_FLASH,  # $0.075/1M
            "gemini-2.0-flash-lite",
            "gemini-2.0-flash",
            "gemini-1.5-flash",
            "gemini-2.5-pro",
        ]

    selected_model = MODEL_GEMINI_FLASH
    for candidate in candidates:
        if getattr(settings, "SURREALDB_OFFLINE", False) or os.getenv("CI"):
            selected_model = candidate
            break

        # Check real-time pricing and regional validity
        rt = resolve_realtime_pricing(candidate)
        if rt:
            selected_model = candidate
            break

        if candidate in {MODEL_GEMINI_FLASH, MODEL_GEMINI_FLASH_LITE}:
            selected_model = candidate
            break

    cache.set(cache_key, selected_model, 86400)
    return selected_model


def _resolve_model_name(model_name: str | None) -> str:
    from extractor.models import SystemSettings

    if not model_name:
        try:
            settings_obj = SystemSettings.get_settings()
            model_name = settings_obj.selected_model
        except Exception:
            model_name = "auto"
    else:
        model_name = str(model_name)

    # Strip the google/ prefix for direct Vertex calls
    if model_name.startswith(PREFIX_GOOGLE):
        model_name = model_name.replace(PREFIX_GOOGLE, "")

    # Guard against stale / invalid Gemini model names persisted in the DB.
    # OpenRouter models contain '/' — those are intentionally non-Gemini, skip.
    if "/" not in model_name and model_name not in KNOWN_GEMINI_MODELS:
        cheapest_fallback = get_cheapest_regional_gemini_model()
        logger.warning(
            "[Gateway] Unknown Gemini model '%s' in configuration. Falling back to dynamic cheapest model '%s'.",
            model_name,
            cheapest_fallback,
        )
        model_name = cheapest_fallback

    return model_name


def _determine_api_routing(model_name: str, is_vision: bool, openrouter_api_key: str) -> tuple[str, bool]:
    final_model = model_name
    use_openrouter = False

    if model_name == "auto":
        if is_vision:
            # Dynamically query and select the cheapest vision-capable Gemini model in the current region
            final_model = get_cheapest_regional_gemini_model(is_vision=True)
        else:
            if openrouter_api_key:
                final_model = MODEL_OPENROUTER_FREE
                use_openrouter = True
            else:
                # Dynamically query and select the cheapest Gemini model in the current region
                final_model = get_cheapest_regional_gemini_model(is_vision=False)
    else:
        # Check if the requested model is an OpenRouter model (has '/' but is not google/)
        if "/" in model_name and not model_name.startswith(PREFIX_GOOGLE):
            use_openrouter = True

    return final_model, use_openrouter


_CASCADABLE_TERMS = (
    _NOT_FOUND,
    "not_found",
    "invalid",
    "deprecated",
    "retired",
    "permission",
    "404",
    "400",
    "429",
    "resource_exhausted",
    "rate limit",
    "quota",
)


def _is_cascadable_error(err: Exception) -> bool:
    err_msg = str(err).lower()
    return any(term in err_msg for term in _CASCADABLE_TERMS)


def _try_direct_gemini_models(
    prompt: str, system_instruction: str | None, files: list[Any] | None, fallback_list: list[str]
) -> tuple[Any | None, Exception | None]:
    last_error = None
    for attempt_model in fallback_list:
        try:
            logger.info("[Gateway] Attempting direct Gemini call using model: %s", attempt_model)
            return _call_direct_gemini(prompt, system_instruction, attempt_model, files), None
        except Exception as e:
            last_error = e
            if _is_cascadable_error(e):
                logger.warning(
                    "[Gateway WARNING] Model '%s' failed or rate-limited: %s. Cascading to next available stable model...",
                    attempt_model,
                    e,
                )
                continue
            raise e
    return None, last_error


def _call_gemini_with_fallback(
    prompt: str,
    system_instruction: str | None,
    final_model: str,
    files: list[Any] | None,
    openrouter_api_key: str,
) -> UnifiedResponse:
    gemini_model = final_model
    if gemini_model.startswith(PREFIX_GOOGLE):
        gemini_model = gemini_model.replace(PREFIX_GOOGLE, "")

    fallback_list = []
    for candidate in [gemini_model, MODEL_GEMINI_FLASH, MODEL_GEMINI_FLASH_LITE]:
        if candidate not in fallback_list:
            fallback_list.append(candidate)

    response, last_error = _try_direct_gemini_models(prompt, system_instruction, files, fallback_list)
    if response:
        return response

    # If we reach here, all direct Gemini options failed.
    # Fall back to OpenRouter free router if the key is configured to maintain 100% service uptime
    if openrouter_api_key:
        logger.warning(
            "[Gateway WARNING] All direct Gemini models failed or rate-limited. Triggering Cross-Provider Failover to OpenRouter..."
        )
        try:
            # OpenRouter Free Router dynamically selects the best available free model
            return _call_openrouter(prompt, system_instruction, MODEL_OPENROUTER_FREE)
        except Exception:
            logger.exception("[Gateway CRITICAL] OpenRouter fallback also failed.")
            if last_error:
                raise last_error
            raise RuntimeError("LLM request failed with no specific error.")

    if last_error:
        raise last_error
    raise RuntimeError("LLM request failed with no specific error.")


def generate_llm_content_unified(
    prompt: str,
    system_instruction: str | None = None,
    model_name: str | None = None,
    files: list[Any] | None = None,
) -> UnifiedResponse:
    """
    Unified LLM API gateway supporting direct Google Gemini and OpenRouter (via HTTP).
    Features:
      1. Intelligent Auto-Routing (routes vision/heavy files to Gemini and standard text tasks to OpenRouter free if key exists).
      2. Progressive Deprecation Fallback Chain (catches retired/deleted Gemini models and cascades to stable alternatives).
      3. Accurate token usage and USD cost tracking.
      4. NFC Unicode normalisation applied to prompt before dispatch.
      5. Structured JSON query result generation.
    """
    import unicodedata

    # Normalise Unicode (e.g. Arabic/Harakat) to NFC to prevent diacritic stripping
    prompt = unicodedata.normalize("NFC", prompt)
    if system_instruction:
        system_instruction = unicodedata.normalize("NFC", system_instruction)

    model_name = _resolve_model_name(model_name)
    openrouter_api_key = _get_openrouter_api_key()

    final_model, use_openrouter = _determine_api_routing(model_name, bool(files), openrouter_api_key)

    # Execution based on API Provider
    if use_openrouter:
        if not openrouter_api_key:
            # Try to run via direct Gemini fallback if key is missing
            logger.warning(
                "[Gateway] OpenRouter model selected but OPENROUTER_API_KEY is missing. Falling back to Gemini."
            )
            return generate_llm_content_unified(prompt, system_instruction, MODEL_GEMINI_FLASH, files)

        try:
            return _call_openrouter(prompt, system_instruction, final_model)
        except Exception as or_err:
            logger.warning(
                f"[Gateway] OpenRouter request failed for model {final_model}: {or_err}. "
                f"Falling back to direct Gemini model {MODEL_GEMINI_FLASH} for resilience."
            )
            return generate_llm_content_unified(prompt, system_instruction, MODEL_GEMINI_FLASH, files)
    else:
        return _call_gemini_with_fallback(prompt, system_instruction, final_model, files, openrouter_api_key)


def extract_retry_delay(exception: Exception) -> float | None:
    """Parses the exception string to find a suggested retry delay in seconds."""
    err_str = str(exception)

    # Check "Please retry in X.XXs" pattern
    match = re.search(r"retry in (\d+(?:\.\d*)?)s", err_str, re.IGNORECASE)
    if match:
        return float(match.group(1))

    # Check "retryDelay": "XXs" pattern
    match_json = re.search(r"['\"]retryDelay['\"]\s*:\s*['\"](\d+(?:\.\d*)?)s?['\"]", err_str, re.IGNORECASE)
    if match_json:
        return float(match_json.group(1))

    return None


def is_rate_limit_error(exception: Exception) -> bool:
    """
    Check if an exception represents a rate limit / RESOURCE_EXHAUSTED / quota exceeded error.
    """
    # Check status code if present (google-genai APIError has status_code or code)
    status_code = getattr(exception, "status_code", None) or getattr(exception, "code", None)
    if status_code == 429:
        return True

    err_msg_lower = str(exception).lower()
    return any(
        term in err_msg_lower
        for term in [
            "429",
            "resource_exhausted",
            "resourceexhausted",
            "rate limit",
            "quota",
            "limit exceeded",
            "spend cap",
            "budget exceeded",
        ]
    )


# Ordered list of Vertex AI regions to try when a model is unavailable in the
# primary region.  asia-southeast1 (Singapore) is closest to Indonesia; if a
# model hasn't been deployed there yet we cascade to us-central1 (global hub)
# then europe-west4 (Netherlands) as a final option.
# Override via settings.VERTEX_REGION_FALLBACK_CHAIN or the env variable.
VERTEX_REGION_FALLBACK_CHAIN: list[str] = getattr(settings, "VERTEX_REGION_FALLBACK_CHAIN", None) or [
    "europe-west9",  # Paris — GDPR-compliant, primary region for Gemini 3.1 models
    "europe-west4",  # Netherlands — GDPR-compliant fallback
    "us-central1",  # Iowa — universal fallback, all models available
    "asia-southeast1",  # Singapore — backup region
]


def get_vertex_client_for_location(location: str) -> Any | None:
    """
    Initializes a Vertex AI GenAI client for the given GCP *location*.
    Returns None if the project cannot be resolved or initialization fails.
    """
    try:
        from google import genai
    except ImportError:
        logger.warning("[Gateway] google-genai package is not installed.")
        return None

    project = (
        getattr(settings, "GCP_PROJECT", None)
        or os.getenv("GCP_PROJECT")
        or os.getenv("GOOGLE_CLOUD_PROJECT")
        or os.getenv("GCP_PROJECT_ID")
    )
    if not project:
        logger.warning("[Gateway] GCP project identifier not set. Vertex AI skipped.")
        return None

    try:
        # Vertex AI uses ADC / service-account credentials (not API keys).
        # Passing api_key alongside project+location raises ValueError; always omit it.
        return genai.Client(vertexai=True, project=project, location=location)
    except Exception as e:
        logger.exception("[Gateway] Failed to init Vertex AI client for location %s: %s", location, e)
        return None


def get_vertex_client() -> Any | None:
    """
    Initializes and returns a Google GenAI Client with vertexai=True,
    using the *primary* region (GCP_REGION setting or first entry in VERTEX_REGION_FALLBACK_CHAIN).
    Returns None if necessary configurations are missing.
    """
    primary_location = (
        getattr(settings, "GCP_REGION", None)
        or os.getenv("GCP_REGION")
        or (VERTEX_REGION_FALLBACK_CHAIN[0] if VERTEX_REGION_FALLBACK_CHAIN else "us-central1")
    )
    client = get_vertex_client_for_location(primary_location)
    if client:
        logger.info("[Gateway] Initialized Vertex AI Client (primary region: %s).", primary_location)
    return client


def execute_embed_content_with_fallback(
    model_name: str,
    contents: list[str],
) -> Any:
    """
    Retrieves embeddings for the given contents, trying first with Vertex AI
    across the region fallback chain, and falling back to AI Studio if necessary.
    """
    # 1. Prioritise Vertex AI via ADC across regions fallback chain
    for region in VERTEX_REGION_FALLBACK_CHAIN:
        vertex_client = get_vertex_client_for_location(region)
        if vertex_client:
            try:
                logger.info(f"[Embed Gateway] Fetching embeddings using Vertex AI in {region}...")
                return execute_with_backoff(vertex_client.models.embed_content, model=model_name, contents=contents)
            except Exception as e:
                logger.warning(f"[Embed Gateway] Vertex AI embedding in region {region} failed: {e}.")

    # 2. Fallback to AI Studio key
    api_key = getattr(settings, "GEMINI_API_KEY", "")
    if api_key and api_key.strip().lower() in ["", "none", "your-gemini-api-key", "placeholder"]:
        api_key = None

    if api_key:
        try:
            logger.info("[Embed Gateway] Fetching embeddings using AI Studio fallback...")
            client = genai.Client(api_key=api_key)
            return execute_with_backoff(client.models.embed_content, model=model_name, contents=contents)
        except Exception as e:
            logger.warning(f"[Embed Gateway] AI Studio embedding failed: {e}.")

    raise ValueError("Embeddings generation failed: both Vertex AI and AI Studio pathways are exhausted.")


def execute_with_backoff(func: Any, *args: Any, max_retries: int = 5, initial_delay: int = 5, **kwargs: Any) -> Any:
    """
    Executes a Google Gemini API function with exponential backoff and dynamic delay parsing
    to handle RPM limits, TPM quota breaches, and temporary failures.
    """
    delay = initial_delay
    for attempt in range(1, max_retries + 1):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            is_rate_limit = is_rate_limit_error(e)
            if attempt == max_retries or not is_rate_limit:
                raise e

            # Try to extract the exact retry delay from the API error response
            suggested_delay = extract_retry_delay(e)
            if suggested_delay is not None:
                # Add a 1.5 second safety margin
                sleep_time = suggested_delay + 1.5
                logger.warning(
                    "[Gemini API] Quota/Rate limit hit. API suggested retrying in %ss. Sleeping for %ss (Attempt %s/%s)...",
                    suggested_delay,
                    sleep_time,
                    attempt,
                    max_retries,
                )
            else:
                sleep_time = delay
                logger.warning(
                    "[Gemini API] Rate limit hit. Retrying in %ss (Attempt %s/%s)...", sleep_time, attempt, max_retries
                )
                delay *= 2

            time.sleep(sleep_time)


def _get_mime_type(file_path: str) -> str:
    """Resolve MIME type from file extension, falling back if mimetypes fails."""
    import mimetypes

    mime_type, _ = mimetypes.guess_type(file_path)
    if mime_type:
        return mime_type
    lowered = file_path.lower()
    if lowered.endswith(".pdf"):
        return MIME_PDF
    if lowered.endswith((".png", ".jpg", ".jpeg", ".webp")):
        return "image/" + lowered.split(".")[-1].replace("jpg", "jpeg")
    return MIME_OCTET_STREAM


def _is_file_ref(item: Any) -> bool:
    """Helper to detect if an item represents a Gemini remote File reference."""
    try:
        if hasattr(item, "uri") or (hasattr(item, "name") and isinstance(item, types.File)):
            return True
    except (AttributeError, TypeError) as exc:
        logger.debug("[LLM Gateway] Item is not a standard File reference: %s", exc)
    try:
        if type(item).__name__ == "File" or hasattr(item, "state"):
            return True
    except (AttributeError, TypeError) as exc:
        logger.debug("[LLM Gateway] Item state check failed: %s", exc)
    return False


def _prepare_vertex_contents(contents: list[Any], file_path_for_vertex: str | None) -> list[Any]:
    if not file_path_for_vertex or not os.path.exists(file_path_for_vertex):
        return contents

    mime_type = _get_mime_type(file_path_for_vertex)

    try:
        with open(file_path_for_vertex, "rb") as f:
            file_bytes = f.read()

        new_contents = []
        for item in contents:
            if _is_file_ref(item):
                logger.info(
                    f"[Gateway] Replacing remote file reference '{getattr(item, 'name', '')}' with inline binary Part for Vertex AI fallback."
                )
                new_contents.append(types.Part.from_bytes(data=file_bytes, mime_type=mime_type))
            else:
                new_contents.append(item)
        return new_contents
    except Exception:
        logger.exception("[Gateway] Failed to load inline file bytes for Vertex fallback.")
        return contents


def _execute_vertex_fallback(fallback_list: list[str], config: Any, vertex_contents: list[Any]) -> tuple[Any, str]:
    """
    Attempts generate_content across both model names AND Vertex AI regions.

    Strategy:
      For each region in VERTEX_REGION_FALLBACK_CHAIN:
        For each model in fallback_list:
          - If the model succeeds → return immediately.
          - If NOT_FOUND / region-unavailable → try next region for that model.
          - Other errors (rate limit, quota) → try next model in same region.

    This means asia-southeast1 is tried first (Singapore, closest to Indonesia).
    If any model isn't available there, us-central1 is the universal fallback.
    """
    NOT_FOUND_SIGNALS = ("not found", "not_found", "404", "does not have access", "not available in")

    last_error: Exception | None = None

    for region in VERTEX_REGION_FALLBACK_CHAIN:
        region_client = get_vertex_client_for_location(region)
        if region_client is None:
            logger.warning("[Gateway/Vertex] Could not initialize client for region %s, skipping.", region)
            continue

        for attempt_model in fallback_list:
            try:
                logger.info("[Gateway/Vertex] Trying model '%s' in region '%s'.", attempt_model, region)
                response = execute_with_backoff(
                    region_client.models.generate_content,
                    model=attempt_model,
                    contents=vertex_contents,
                    config=config,
                )
                logger.info("[Gateway/Vertex] Success: model '%s' in region '%s'!", attempt_model, region)
                return response, attempt_model
            except Exception as ve:
                err_str = str(ve).lower()
                is_region_gap = any(sig in err_str for sig in NOT_FOUND_SIGNALS)
                if is_region_gap:
                    logger.warning(
                        "[Gateway/Vertex] Model '%s' not available in '%s' — trying next model.",
                        attempt_model,
                        region,
                    )
                    last_error = ve
                    continue
                else:
                    logger.warning(
                        "[Gateway/Vertex] Model '%s' failed in '%s': %s — trying next model.",
                        attempt_model,
                        region,
                        ve,
                    )
                    last_error = ve
                    continue

    raise last_error or RuntimeError("All Vertex AI regions and models exhausted.")


class CascadableModelError(Exception):
    """Internal exception to signal that a model failed with a cascadable error."""


def _attempt_generate_content(
    client: Any, attempt_model: str, contents: list[Any], config: Any
) -> tuple[Any, str] | None:
    """
    Attempts to generate content using a single model.
    Returns (response, model) on success.
    Returns None if rate-limited (stops cascade to trigger Vertex fallback).
    Raises CascadableModelError if model failed with a temporary/cascadable error.
    """
    try:
        logger.info(f"[Gateway] Attempting generate_content using model: {attempt_model}")
        response = execute_with_backoff(
            client.models.generate_content, model=attempt_model, contents=contents, config=config
        )
        return response, attempt_model
    except Exception as e:
        if is_rate_limit_error(e):
            logger.warning(f"[Gateway] AI Studio model '{attempt_model}' rate-limited or exhausted: {e}.")
            return None

        err_msg = str(e).lower()
        is_cascadable = any(
            term in err_msg
            for term in [
                _NOT_FOUND,
                "not_found",
                "invalid",
                "deprecated",
                "retired",
                "permission",
                "404",
                "400",
                "500",
                "503",
                "internal",
                "unavailable",
                "service unavailable",
            ]
        )
        if is_cascadable:
            logger.warning(
                f"[Gateway WARNING] Model '{attempt_model}' failed: {e}. Cascading to next available stable model..."
            )
            raise CascadableModelError(e) from e
        raise e


def execute_generate_content_with_fallback(
    client: Any,
    model_name: str,
    contents: list[Any],
    config: Any = None,
    file_path_for_vertex: str | None = None,
) -> tuple[Any, str]:
    """
    Executes client.models.generate_content with fallback candidates
    if the main model fails due to deprecation, quota, or network errors.
    If the active client is rate-limited or exhausted (429), it automatically
    attempts Vertex AI fallback.
    Returns a tuple of (response_object, actual_model_used).
    """
    model_name = _resolve_model_name(model_name)
    if model_name == "auto":
        model_name = MODEL_GEMINI_FLASH

    fallback_list = []
    # Fallback priority: chosen model -> stable Flash -> stable Flash-Lite.
    for candidate in [model_name, MODEL_GEMINI_FLASH, MODEL_GEMINI_FLASH_LITE]:
        if candidate not in fallback_list:
            fallback_list.append(candidate)

    last_error = None
    ai_studio_rate_limited = False

    for attempt_model in fallback_list:
        try:
            result = _attempt_generate_content(client, attempt_model, contents, config)
            if result is None:
                ai_studio_rate_limited = True
                break
            return result
        except CascadableModelError as exc:
            last_error = exc.__cause__
        except Exception as exc:
            last_error = exc
            raise exc

    # If AI Studio is rate limited or exhausted, try Vertex AI fallback
    if ai_studio_rate_limited or (isinstance(last_error, Exception) and is_rate_limit_error(last_error)):
        logger.warning("[Gateway] Attempting Vertex AI fallback due to AI Studio rate limits or resource exhaustion...")
        vertex_contents = _prepare_vertex_contents(contents, file_path_for_vertex)
        return _execute_vertex_fallback(fallback_list, config, vertex_contents)

    if last_error:
        raise last_error
    raise RuntimeError("Multimodal OCR pipeline failed with no specific error.")


def _init_ocr_client() -> tuple[Any, bool]:
    if os.getenv("K_SERVICE"):
        logger.info("[OCR Stage 1] Running on GCP Cloud Run. Initializing Vertex AI via ADC.")
        client = get_vertex_client()
        if client:
            return client, True

    api_key = settings.GEMINI_API_KEY
    client = None
    if api_key:
        try:
            client = genai.Client(api_key=api_key)
            return client, False
        except Exception as ce:
            logger.warning(f"[OCR Stage 1] Failed to initialize AI Studio client: {ce}. Trying Vertex directly.")

    client = get_vertex_client()
    if not client:
        raise ValueError(VAL_ERROR_UNAVAILABLE)
    return client, True


def _run_ocr_vertex(client: Any, file_path: str, model_name: str, ocr_prompt: str) -> tuple[Any, str]:
    import mimetypes

    mime_type, _ = mimetypes.guess_type(file_path)
    if not mime_type:
        if file_path.lower().endswith(".pdf"):
            mime_type = MIME_PDF
        elif file_path.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            mime_type = "image/" + file_path.lower().split(".")[-1].replace("jpg", "jpeg")
        else:
            mime_type = MIME_OCTET_STREAM

    with open(file_path, "rb") as f:
        file_bytes = f.read()

    contents = [types.Part.from_bytes(data=file_bytes, mime_type=mime_type), ocr_prompt]
    return execute_generate_content_with_fallback(client, model_name, contents=contents, file_path_for_vertex=file_path)


def _poll_gemini_file(client: Any, file_ref: Any, file_path: str) -> Any:
    max_polls = 60
    poll_count = 0
    while file_ref.state.name == "PROCESSING":
        poll_count += 1
        if poll_count > max_polls:
            try:
                client.files.delete(name=file_ref.name)
            except Exception as cleanup_err:
                logger.warning(
                    "[OCR Stage 1] Failed to delete remote file %s on timeout: %s", file_ref.name, cleanup_err
                )
            raise GeminiProcessingError(f"Gemini Files API file processing timed out for file: {file_path}")
        time.sleep(5)
        file_ref = client.files.get(name=file_ref.name)

    if file_ref.state.name == "FAILED":
        raise GeminiProcessingError(f"Gemini Files API file processing failed for file: {file_path}")
    return file_ref


def _run_ocr_with_upload(client: Any, file_path: str, model_name: str, ocr_prompt: str) -> tuple[Any, str]:
    """Uploads the file to Gemini Files API and runs OCR, with Vertex AI fallback on rate limits."""
    file_ref = None
    try:
        logger.info("[OCR Stage 1] Uploading %s to Gemini Files API...", os.path.basename(file_path))
        file_ref = client.files.upload(file=file_path)

        # Poll file processing status
        logger.info("[OCR Stage 1] Polling processing state for %s...", file_ref.name)
        file_ref = _poll_gemini_file(client, file_ref, file_path)

        logger.info("[OCR Stage 1] File ready. Processing with %s...", model_name)

        return execute_generate_content_with_fallback(
            client, model_name, contents=[file_ref, ocr_prompt], file_path_for_vertex=file_path
        )
    except Exception as e:
        if is_rate_limit_error(e):
            logger.warning(
                "[OCR Stage 1] AI Studio rate limited during file upload or generation. Attempting Vertex AI fallback..."
            )
            vertex_client = get_vertex_client()
            if vertex_client:
                return _run_ocr_vertex(vertex_client, file_path, model_name, ocr_prompt)
        raise e
    finally:
        if file_ref:
            try:
                client.files.delete(name=file_ref.name)
                logger.info("[OCR Stage 1] Cleaned up file %s from Gemini servers.", file_ref.name)
            except Exception as cleanup_err:
                logger.warning("[OCR Stage 1] Failed to delete remote file %s: %s", file_ref.name, cleanup_err)


def run_stage1_multimodal_ocr(file_path: str, model_name: str = MODEL_GEMINI_FLASH_LITE) -> dict[str, Any]:
    """
    Pass 1 Multimodal OCR. Uploads the target PDF/Image using Gemini Files API
    to handle heavy payloads (up to 170+ pages) without timeouts or memory crashes,
    runs structure-aware optical character recognition, and monitors execution.
    Defaults to MODEL_GEMINI_FLASH_LITE (gemini-2.5-flash-lite) for high-speed,
    cost-optimized batch ingestion.
    If Gemini API Key is missing or rate-limited/exhausted, falls back to Vertex AI inline bytes.
    """
    model_name = _resolve_model_name(model_name)
    if model_name == "auto":
        model_name = MODEL_GEMINI_FLASH_LITE

    client, use_vertex_directly = _init_ocr_client()

    ocr_prompt = """
    You are an expert OCR and document layout conversion engine.
    Your task is to transcribe the attached document into clean, readable Markdown.

    Rules:
    1. Transcribe the document page-by-page. Preserve ALL linguistic characters EXACTLY as written.
       Arabic text with full vowel diacritics (Harakat: ًٌٍَُِِّْ) MUST be preserved verbatim with NFC Unicode normalisation.
       Never strip diacritics, never reorder characters, never replace Arabic letters with ASCII approximations.
    2. Maintain layout structures, tables, lists, and indentation. Preserve headers, section numbers, and blockquotes.
    3. If there are tables, output them in clean Markdown table formatting.
    4. Do NOT paraphrase, summarize, or truncate any part of the text. Keep all contents.
    5. If a page contains decorative borders, drawings, or calligraphy background containing no text, ignore the background graphic but transcribe all readable textual scripts.
    6. For bilingual side-by-side structures, render them in appropriate layout (such as consecutive paragraphs, or tables if aligned).
    7. Output ONLY the transcribed Markdown. Do not add commentary or explanations about the document.
    """

    if use_vertex_directly:
        logger.info("[OCR Stage 1/Vertex] Running OCR via Vertex AI (inline bytes)...")
        response, actual_model = _run_ocr_vertex(client, file_path, model_name, ocr_prompt)
    else:
        response, actual_model = _run_ocr_with_upload(client, file_path, model_name, ocr_prompt)

    input_toks = response.usage_metadata.prompt_token_count or 0
    output_toks = response.usage_metadata.candidates_token_count or 0
    cost = calculate_gemini_cost(actual_model, input_toks, output_toks)

    raw_markdown = response.text or ""
    if not raw_markdown:
        logger.warning("[OCR Stage 1] LLM returned empty/None text. Document may have no extractable content.")

    return {"raw_markdown": raw_markdown, "input_tokens": input_toks, "output_tokens": output_toks, "cost_usd": cost}


def _init_refinement_client() -> Any:
    if os.getenv("K_SERVICE"):
        logger.info("[Refinement Pass 2] Running on GCP Cloud Run. Initializing Vertex AI via ADC.")
        client = get_vertex_client()
        if client:
            return client

    api_key = settings.GEMINI_API_KEY
    if api_key:
        try:
            return genai.Client(api_key=api_key)
        except Exception as ce:
            logger.warning(f"[Refinement Pass 2] Failed to initialize AI Studio client: {ce}. Trying Vertex.")

    client = get_vertex_client()
    if not client:
        raise ValueError(VAL_ERROR_UNAVAILABLE)
    return client


def _parse_yaml_block(refined_text: str) -> tuple[str, str]:
    yaml_block = ""
    # Case 1: YAML block wrapped inside standard markdown code block fences (e.g. ```yaml ... ```)
    code_block_match = re.match(r"^```(?:yaml)?[ \t]*\n(.*)\n```", refined_text, re.DOTALL | re.IGNORECASE)
    if code_block_match:
        yaml_content = code_block_match.group(1).strip()
        # Clean any inner ---\n...\n--- if present in code block
        if yaml_content.startswith("---"):
            yaml_content = re.sub(r"^---+[ \t]*\n(.*)\n---+", r"\1", yaml_content, flags=re.DOTALL)
        yaml_block = yaml_content
        refined_text = refined_text[code_block_match.end() :].lstrip("\n")
    else:
        # Case 2: Standard YAML frontmatter block starting and ending with ---
        yaml_match = re.search(
            r"^---+[ \t]*\n(?P<yaml>.*?)---+[ \t]*(?:\n|$)",
            refined_text,
            re.MULTILINE | re.DOTALL,
        )
        if yaml_match:
            yaml_block = yaml_match.group("yaml").strip()
            refined_text = refined_text[yaml_match.end() :]
        else:
            # Case 3: YAML block starting directly with key-value pairs at beginning and ending with ---
            direct_match = re.search(
                r"^(?P<yaml>(?:\w+[ \t]*:[^\n]*\n)+)---+[ \t]*(?:\n|$)",
                refined_text,
                re.MULTILINE,
            )
            if direct_match:
                yaml_block = direct_match.group("yaml").strip()
                refined_text = refined_text[direct_match.end() :]
            else:
                # Case 4: Standard search fallback
                yaml_match = re.search(
                    r"^---+[ \t]*\n(?P<yaml>.*?)---+",
                    refined_text,
                    re.MULTILINE | re.DOTALL,
                )
                if yaml_match:
                    yaml_block = yaml_match.group("yaml").strip()
                    refined_text = refined_text[yaml_match.end() :].lstrip("\n")
    return yaml_block, refined_text


def _parse_refinement_output(full_output: str | None) -> tuple[str, str, list[Any]]:
    """Parse Stage 2 LLM output into (refined_text, yaml_block, qa_list).

    Guards against None output which can occur when all model fallbacks are
    exhausted or when the LLM returns a safety-blocked empty response.
    Also handles common LLM formatting variations for the YAML front-matter block.
    """
    if not full_output:
        logger.warning("[Refinement Pass 2] LLM returned empty/None response. Returning empty parsed result.")
        return "", "", []

    refined_text = full_output.strip()
    yaml_block, refined_text = _parse_yaml_block(refined_text)

    qa_list = []
    # Find JSON Q&A block
    json_match = re.search(r"`{3,4}json[ \t]*\n(.*)\n`{3,4}", refined_text, re.DOTALL)
    if json_match:
        try:
            qa_list = json.loads(json_match.group(1))
        except Exception:
            logger.exception("[Refinement Pass 2] JSON Parsing error")

        pre_json = refined_text[: json_match.start()].rstrip()
        pre_json = re.sub(r"(?:\r?\n)#[^\r\n]*", "", pre_json).rstrip()
        refined_text = pre_json

    # Clean any trailing or leading stray backticks, spaces, or lines from markdown split/model boundaries
    refined_text = refined_text.strip().lstrip("` \n\r").rstrip("` \n\r").strip()

    return refined_text, yaml_block.strip(), qa_list


def run_stage2_editorial_refinement(raw_markdown: str, model_name: str = MODEL_GEMINI_FLASH) -> dict[str, Any]:
    """
    Pass 2 Reasoning Curation Engine. Removes headers, footers, page numbering,
    re-joins sentences, structures metadata YAML Front-matter block, and
    compiles a high-quality SFT Q&A dataset of 3-10 question/answer pairs in JSON.
    """
    model_name = _resolve_model_name(model_name)
    if model_name == "auto":
        model_name = MODEL_GEMINI_FLASH

    client = _init_refinement_client()

    refinement_prompt = """
    You are a Digital Preservation Librarian and Archival Scholar.
    Review the following raw transcribed Markdown text. Perform the following actions:
    
    1. Strip all redundant headers, footers, page margins, and page numbers from the output text.
    2. Fix sentences or words that were awkwardly split across page margins or lines (e.g. joining split words, restoring hyphenations).
    3. Organize the layout with neat, clean hierarchy headings (#, ##, ###). Ensure list formatting, bold markers, and italic annotations are consistent.
    4. You MUST preserve the author's original meaning, intent, and writing style. Do NOT summarize, rewrite, or alter the original points or text body. You are only converting the knowledge format.
    5. At the very top of your response, output a structured YAML Front-matter block wrapped in `---` lines. The YAML block MUST contain the following keys:
       - `title`: The document title in English. If the original is non-Latin, provide an English transliteration or translation. NEVER output "Unknown" — derive the best English title from the content or filename.
       - `author`: The author or publisher name in English. Transliterate non-Latin names using common English spellings. NEVER output "Unknown" — if unsure, write "Anonymous" or the publisher name.
       - `language`: Primary language of the source text (e.g. "Arabic", "English", "Indonesian", etc.).
       - `document_type`: Type of document (e.g. "Treatise", "Academic Paper", "Book", "Lecture Notes").
       - `subject`: A concise English summary subject or theme (MUST be in English).
       - `semantic_signature`: A 64-character hex-like unique signature based on major concepts in the text.
       - `isbn`: International Standard Book Number (ISBN) if present in the text. If not found, write "".
       - `source_link`: The source URL, QR code reference link, or publisher website link if found in the text. If not found, write "".
       - `translator`: Translator or editor name in English if the booklet is translated. If not found, write "".
       - `publisher`: The publisher of the document, if available. Write "Unknown" if not found.
       - `publication_year`: The year of publication (4 digits). Write "" if not found.
       - `license_type`: The license or copyright status (e.g., "Public Domain", "Copyrighted", "CC-BY"). Write "Unknown" if not explicitly stated.
       - `doi`: Digital Object Identifier if present. Write "" if not found.
       CRITICAL: Every YAML value MUST be a properly quoted string. Use double quotes around ALL values.
       CRITICAL: NEVER leave title, author, language, document_type, or subject fields as "Unknown" or empty — always make a best-effort inference from the document content. For isbn, source_link, translator, publisher, publication_year, license_type, and doi, use empty strings or "Unknown" as specified if truly not mentioned.
    6. At the very bottom of your response, build an interactive Q&A training dataset based on the knowledge in this text.
       This dataset is for Supervised Fine-Tuning (SFT) or Archival Research.
       It must contain between 3 to 10 question-answer pairs of high complexity.
       IMPORTANT: The questions and answers in this training dataset MUST be written in English by default, regardless of the primary language of the input text.
       Format this dataset in an explicit fenced code block labeled ````json ... ```` containing a JSON list of objects:
       [{"question": "...", "answer": "..."}]

    Input Text:
    """

    # We wrap models call with robust model-fallback chain
    response, actual_model = execute_generate_content_with_fallback(
        client, model_name, contents=[refinement_prompt, raw_markdown]
    )

    input_toks = response.usage_metadata.prompt_token_count or 0
    output_toks = response.usage_metadata.candidates_token_count or 0
    cost = calculate_gemini_cost(actual_model, input_toks, output_toks)

    refined_markdown, yaml_metadata, qa_dataset = _parse_refinement_output(response.text)

    return {
        "refined_markdown": refined_markdown,
        "yaml_metadata": yaml_metadata,
        "qa_dataset": qa_dataset,
        "input_tokens": input_toks,
        "output_tokens": output_toks,
        "cost_usd": cost,
    }


def generate_multimodal_vision_ocr(
    image_bytes: bytes,
    mime_type: str = "image/jpeg",
    prompt: str = "Describe this diagram, schema, or flowchart in detailed structured Markdown.",
) -> str:
    """
    Invokes Gemini 3.6 Flash / Vertex AI Vision to perform Multi-Modal Diagram & Schema OCR.
    Extracts embedded flowcharts, architectural diagrams, tables, and handwritten notes.
    """
    if not image_bytes:
        return ""
    try:
        client, _is_vertex = _init_ocr_client()
        if client:
            part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
            response, _ = execute_generate_content_with_fallback(client, MODEL_GEMINI_FLASH, contents=[part, prompt])
            if response and response.text:
                return response.text.strip()
    except Exception as exc:
        logger.warning("[Vision OCR] Multi-Modal Diagram OCR failed: %s", exc)
    return ""
