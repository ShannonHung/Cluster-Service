# 目標是什麼？

你想讓一個外部程式（例如你自己寫的 API service）能夠呼叫 Kubernetes API，所以需要：

一個身份（它是誰？）
一組權限（它能做什麼？）
一把鑰匙（它怎麼證明自己的身份？）

# 關係圖
```
┌─────────────────────┐
│    ClusterRole      │  定義「能做什麼」
│  ・get nodes        │  （權限清單，不屬於任何人）
│  ・list pods        │
│  ・delete pods      │
└──────────┬──────────┘
           │
           │ ClusterRoleBinding 把兩者綁起來
           │
┌──────────▼──────────┐
│   ServiceAccount    │  定義「這個身份」
│ k8s-cluster-service │  （類似一個 user 帳號）
└──────────┬──────────┘
           │
           │ Secret 綁定這個 ServiceAccount
           │
┌──────────▼──────────┐
│       Secret        │  產生一個 JWT token
│   type: SA token    │  （這就是登入用的鑰匙）
└─────────────────────┘
           │
           ▼
     拿這個 token
     組成 kubeconfig
     給外部程式使用
```

# 組成 kubeconfig 的流程
deploy 完
```
kubectl apply -f namespace.yml
kubectl apply -f service-account.yml
kubectl apply -f cluster-role.yml
kubectl apply -f sa-secret.yml
kubectl apply -f cluster-rolebinding.yml
```

執行以下步驟取得 token：
```
# 1. 取得 token
kubectl get secret k8s-cluster-service-api-service-token \
  -n k8s-api \
  -o jsonpath='{.data.token}' | base64 -d

# 2. 取得 CA 憑證
kubectl get secret k8s-cluster-service-api-service-token \
  -n k8s-api \
  -o jsonpath='{.data.ca\.crt}'

# 3. 取得 API Server 位址
kubectl cluster-info
```

然後組成這個 kubeconfig：
```
apiVersion: v1
kind: Config
clusters:
  - name: my-cluster
    cluster:
      server: https://your-api-server:6443   # ← 來自 cluster-info
      certificate-authority-data: <ca.crt>   # ← 來自 Secret 的 ca.crt

users:
  - name: k8s-cluster-service-api-service
    user:
      token: <token>                         # ← 來自 Secret 的 token

contexts:
  - name: k8s-api-context
    context:
      cluster: my-cluster
      user: k8s-cluster-service-api-service
      namespace: k8s-api

current-context: k8s-api-context
```


# YAML 
> namespace.yml 
```
apiVersion: v1
kind: Namespace
metadata:
  name: k8s-api
```

> service-account.yml 
```
apiVersion: v1
kind: ServiceAccount
metadata:
  name: k8s-cluster-service-api-service
  namespace: k8s-api
```

> cluster-role.yml 
```
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: k8s-cluster-service-api-service-clusterrole
rules:
  - apiGroups: [""]
    resources: ["nodes"]
    verbs: ["get", "list", "watch", "patch"]
  - apiGroups: [""]
    resources: ["pods/eviction"]
    verbs: ["create"]
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get", "list", "delete"]
  - apiGroups: ["apps"]
    resources: ["daemonsets"]
    verbs: ["get"]
```

> sa-secret.yml 
```
apiVersion: v1
kind: Secret
metadata:
  name: k8s-cluster-service-api-service-token
  namespace: k8s-api
  annotations:
    kubernetes.io/service-account.name: k8s-cluster-service-api-service
type: kubernetes.io/service-account-token
```

> cluster-rolebinding.yml 
```
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: k8s-cluster-service-api-service-clusterrolebinding
subjects:
  - kind: ServiceAccount
    name: k8s-cluster-service-api-service
    namespace: k8s-api
roleRef:
  kind: ClusterRole
  name: k8s-cluster-service-api-service-clusterrole
  apiGroup: rbac.authorization.k8s.io
```