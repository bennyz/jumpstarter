from typing import Annotated, Any, Dict

from kubernetes_asyncio.client.models import V1Condition, V1ObjectMeta, V1ObjectReference
from pydantic import WrapSerializer


def k8s_obj_to_dict(value: Any, handler, info) -> Dict[str, Any]:
    return value.to_dict(serialize=True)


SerializeV1Condition = Annotated[V1Condition, WrapSerializer(k8s_obj_to_dict)]
SerializeV1ObjectMeta = Annotated[V1ObjectMeta, WrapSerializer(k8s_obj_to_dict)]
SerializeV1ObjectReference = Annotated[V1ObjectReference, WrapSerializer(k8s_obj_to_dict)]
