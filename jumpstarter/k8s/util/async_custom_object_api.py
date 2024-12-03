from contextlib import AbstractAsyncContextManager
from typing import Any, Coroutine, Optional, Self

from kubernetes_asyncio import config
from kubernetes_asyncio.client.api import CustomObjectsApi
from kubernetes_asyncio.client.api_client import ApiClient


class AbstractAsyncCustomObjectApi(AbstractAsyncContextManager):
    """An abstract async custom object API client"""

    _client: ApiClient
    config_file: Optional[str]
    context: Optional[str]
    namespace: str
    api: CustomObjectsApi

    def __init__(self, namespace: str, config_file: Optional[str] = None, context: Optional[str] = None):
        self.config_file = config_file
        self.context = context
        self.namespace = namespace

    async def __aenter__(self) -> Self:
        # Load the kubeconfig
        await config.load_kube_config(self.config_file, self.context)
        # Construct the API client and enter context
        self._client = ApiClient()
        await self._client.__aenter__()
        # Construct the custom objects API client
        self.api = CustomObjectsApi(self._client)
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self._client.__aexit__(exc_type, exc_value, traceback)
        self._client = None
        self.api = None
        return None
