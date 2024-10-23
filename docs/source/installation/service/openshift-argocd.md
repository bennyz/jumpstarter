# OpenShift (ArgoCD)

## Create namespace
First, we must create a namespace for the Jumpstarter installation. This namespace
should be labeled with `argocd.argoproj.io/managed-by=<your-argo-CD-instance>` to allow
ArgoCD to manage the resources in the namespace.

In this case, using the default openshift-gitops ArgoCD deployment, the command would be:
```bash
kubectl create namespace jumpstarter-lab
kubectl label namespace jumpstarter-lab argocd.argoproj.io/managed-by=openshift-gitops
```

## Note on CRDs

ArgoCD needs to be able to manage the CRDs that Jumpstarter uses. This is done by creating a ClusterRole and ClusterRoleBinding that allows the ArgoCD application controller to manage the CRDs.

An alternative to this is to manually create and update the CRDs
that jumpstarter uses.

The ClusterRole & Binding to allow ArgoCD to manage the CRDs are:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  annotations:
    argocds.argoproj.io/name: openshift-gitops
    argocds.argoproj.io/namespace: openshift-gitops
  name: openshift-gitops-argocd-appcontroller-crd
rules:
- apiGroups:
  - 'apiextensions.k8s.io'
  resources:
  - 'customresourcedefinitions'
  verbs:
  - '*'
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  annotations:
    argocds.argoproj.io/name: openshift-gitops
    argocds.argoproj.io/namespace: openshift-gitops
  name: openshift-gitops-argocd-appcontroller-crd
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: openshift-gitops-argocd-appcontroller-crd
subjects:
- kind: ServiceAccount
  name: openshift-gitops-argocd-application-controller
  namespace: openshift-gitops
```

## Application

```{warning}
`jumpstarter-controller.controllerSecret` and `jumpstarter-controller.routerSecret`
are secrets that are used to secure the authentication between clients and the jumpstarter elements.
**These secrets should be unique and not shared between installations**. Helm installation can
auto-generate values for these, but with ArgoCD such mechanism doesn't work. You need to manually
create these secrets in the namespace where the jumpstarter is installed.
```

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: jumpstarter
  namespace: openshift-gitops
spec:
  destination:
    name: in-cluster
    namespace: jumpstarter-lab
  project: default
  source:
    chart: jumpstarter
    helm:
      parameters:
      - name: global.baseDomain
        value: devel.jumpstarter.dev
      - name: global.metrics.enabled
        value: "true"
      - name: jumpstarter-controller.controllerSecret
        value: "pick-a-secret-DONT-USE-THIS-DEFAULT"
      - name: jumpstarter-controller.routerSecret
        value: "again-pick-a-secret-DONT-USE-THIS-DEFAULT"
      - name: jumpstarter-controller.grpc.mode
        value: "route"
    repoURL: quay.io/jumpstarter-dev/helm
    targetRevision: "0.1.0"
```
