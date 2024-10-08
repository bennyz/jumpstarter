# Ported from https://github.com/kubernetes/apimachinery/blob/v0.31.1/pkg/api/meta/conditions.go

from jumpstarter.v1 import kubernetes_pb2


def condition_present_and_equal(conditions: list[kubernetes_pb2.Condition], condition_type: str, status: str) -> bool:
    for condition in conditions:
        if condition.type == condition_type:
            return condition.status == status
    return False


def condition_true(conditions: list[kubernetes_pb2.Condition], condition_type: str) -> bool:
    return condition_present_and_equal(conditions, condition_type, "True")


def condition_false(conditions: list[kubernetes_pb2.Condition], condition_type: str) -> bool:
    return condition_present_and_equal(conditions, condition_type, "False")