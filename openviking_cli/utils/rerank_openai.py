# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""
HTTP rerank client for OpenAI-compatible and DashScope-native endpoints.

Supports third-party rerank services like Alibaba Cloud DashScope via
api_key + api_base configuration.
"""

from typing import Any, Dict, List, Optional

import requests

from openviking_cli.utils.logger import get_logger

logger = get_logger(__name__)


class OpenAIRerankClient:
    """
    Rerank API client using Bearer token auth.

    Compatible with OpenAI/Cohere-style rerank endpoints and Alibaba Cloud
    DashScope's native rerank HTTP endpoint.
    """

    def __init__(self, api_key: str, api_base: str, model_name: str):
        """
        Initialize OpenAI-compatible rerank client.

        Args:
            api_key: Bearer token for authentication
            api_base: Full endpoint URL for the rerank API
            model_name: Model name to use for reranking
        """
        self.api_key = api_key
        self.api_base = api_base.rstrip("/")
        self.model_name = model_name

    def _uses_dashscope_native_shape(self) -> bool:
        """Whether api_base points to DashScope's native rerank endpoint."""
        return "/api/v1/services/rerank/" in self.api_base

    def _build_request_body(self, query: str, documents: List[str]) -> Dict[str, Any]:
        """Build the request body for the configured rerank endpoint."""
        if self._uses_dashscope_native_shape():
            return {
                "model": self.model_name,
                "input": {
                    "query": query,
                    "documents": documents,
                },
            }

        return {
            "model": self.model_name,
            "query": query,
            "documents": documents,
        }

    @staticmethod
    def _extract_results(result: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
        """Extract rerank results from known response shapes."""
        results = result.get("results")
        if results is not None:
            return results

        output = result.get("output")
        if isinstance(output, dict):
            nested_results = output.get("results")
            if nested_results is not None:
                return nested_results

        return None

    def rerank_batch(self, query: str, documents: List[str]) -> Optional[List[float]]:
        """
        Batch rerank documents against a query.

        Args:
            query: Query text
            documents: List of document texts to rank

        Returns:
            List of rerank scores for each document (same order as input),
            or None when rerank fails and the caller should fall back
        """
        if not documents:
            return []

        req_body = self._build_request_body(query, documents)

        try:
            response = requests.post(
                url=self.api_base,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=req_body,
                timeout=30,
            )
            response.raise_for_status()
            result = response.json()

            # OpenAI-compatible shape uses top-level results; DashScope native
            # rerank nests results under output.results.
            results = self._extract_results(result)
            if not results:
                logger.warning(f"[OpenAIRerankClient] Unexpected response format: {result}")
                return None

            if len(results) != len(documents):
                logger.warning(
                    "[OpenAIRerankClient] Unexpected rerank result length: expected=%s actual=%s",
                    len(documents),
                    len(results),
                )
                return None

            # Results may not be in original order — sort by index
            scores = [0.0] * len(documents)
            for item in results:
                idx = item.get("index")
                if idx is None or not (0 <= idx < len(documents)):
                    logger.warning(
                        "[OpenAIRerankClient] Out-of-bounds or missing index in result: %s", item
                    )
                    return None
                scores[idx] = item.get("relevance_score", 0.0)

            logger.debug(f"[OpenAIRerankClient] Reranked {len(documents)} documents")
            return scores

        except requests.HTTPError as e:
            response = e.response
            if response is not None:
                request_id = response.headers.get("x-request-id") or response.headers.get(
                    "x-dashscope-request-id"
                )
                try:
                    error_payload = response.json()
                except ValueError:
                    error_payload = response.text

                logger.error(
                    "[OpenAIRerankClient] Rerank failed: status=%s request_id=%s body=%s",
                    response.status_code,
                    request_id,
                    error_payload,
                )
            else:
                logger.error(f"[OpenAIRerankClient] Rerank failed: {e}")
            return None
        except Exception as e:
            logger.error(f"[OpenAIRerankClient] Rerank failed: {e}")
            return None

    @classmethod
    def from_config(cls, config) -> Optional["OpenAIRerankClient"]:
        """
        Create OpenAIRerankClient from RerankConfig.

        Args:
            config: RerankConfig instance with provider='openai'

        Returns:
            OpenAIRerankClient instance or None if config is not available
        """
        if not config or not config.is_available():
            return None
        return cls(
            api_key=config.api_key,
            api_base=config.api_base,
            model_name=config.model or "qwen3-rerank",
        )
