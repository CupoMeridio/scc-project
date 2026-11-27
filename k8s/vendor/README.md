# Ingress NGINX Vendor Manifests (D3 / V7)

* **Originale upstream non modificato:** `ingress-nginx-controller-v1.15.1.yaml`
* **Origine:** `https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.15.1/deploy/static/provider/cloud/deploy.yaml`
* **Checksum SHA-256 verificabile:** `ingress-nginx-controller-v1.15.1.yaml.sha256`
  (`sha256sum -c k8s/vendor/ingress-nginx-controller-v1.15.1.yaml.sha256`)
* **Patch KinD locale (`kind-ingress-ready.patch`):** aggiunge `nodeSelector: ingress-ready: "true"` al
  solo Deployment `ingress-nginx-controller`, vincolandolo al nodo `scc-cluster-control-plane` dove
  [`k8s/kind-config.yaml`](../kind-config.yaml) mappa le porte host `80/443`.
  `make cluster-up` verifica il checksum dell'originale e applica la patch in memoria senza alterare il file sorgente.
