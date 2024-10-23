# Kubernetes

The Jumpstarter service can be installed on a Kubernetes cluster using Helm.

```{warning}
This configuration hasn't been fully tested, please report any issues you may find.
```

```{note}
Please note that `global.baseDomain` is used to create the host names for the services,
with the provided example the services will be available at grpc.jumpstarter.example.com
```

```bash
    helm upgrade jumpstarter --install oci://quay.io/jumpstarter-dev/helm/jumpstarter \
            --create-namespace --namespace jumpstarter-lab \
            --set global.baseDomain=jumpstarter.example.com \
            --set global.metrics.enabled=true # disable if metrics not available \
            --set jumpstarter-controller.grpc.mode=ingress \
            --version=0.1.0
```