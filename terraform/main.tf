# Terraform for the cluster-side resources that are not application manifests.
#
# **What this deliberately does not do: re-express the manifests.** The Kubernetes provider
# can render every Deployment in HCL, and doing so would mean maintaining two descriptions of
# one system that drift the first time someone edits only one. The manifests in k8s/ are the
# description; Terraform owns the things that sit *around* them and that kubectl is a poor fit
# for -- namespace lifecycle, resource quotas, the secret's existence (not its contents), and
# the ordering between them (ADR-047).
#
# Everything here targets a cluster that already exists. Provisioning the cluster itself is
# the one job that genuinely needs a cloud provider, and it is optional and off by default --
# see providers.tf.

terraform {
  required_version = ">= 1.6"

  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.35"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

# The namespace. Owned here rather than by 00-namespace.yaml so that `terraform destroy`
# removes the whole thing in one step; applying both is harmless because the manifest is
# `kubectl apply`-idempotent and this resource adopts nothing it did not create.
resource "kubernetes_namespace_v1" "vigil" {
  metadata {
    name = var.namespace

    labels = {
      "app.kubernetes.io/part-of"    = "vigil"
      "app.kubernetes.io/managed-by" = "terraform"
    }
  }
}

# A quota, which is the reason Terraform is worth having here at all. The manifests set
# per-container requests and limits; nothing in them stops a careless `kubectl scale` from
# asking for forty replicas. This is the ceiling, and it is sized from the measured footprint
# of the compose stack (1.2 GB across four services) with headroom for the consumers.
resource "kubernetes_resource_quota_v1" "vigil" {
  metadata {
    name      = "vigil-quota"
    namespace = kubernetes_namespace_v1.vigil.metadata[0].name
  }

  spec {
    hard = {
      "requests.cpu"    = var.quota_requests_cpu
      "requests.memory" = var.quota_requests_memory
      "limits.cpu"      = var.quota_limits_cpu
      "limits.memory"   = var.quota_limits_memory
      "pods"            = var.quota_pods
      # The stores ask for 45Gi of PersistentVolumeClaims between them. A cap stops a
      # mistyped volumeClaimTemplate from filling the node.
      "requests.storage" = var.quota_requests_storage
    }
  }
}

# A LimitRange, so a pod that forgets to declare resources gets defaults rather than
# unbounded consumption. Every manifest in k8s/ declares its own; this catches the one that
# someone adds later and forgets.
resource "kubernetes_limit_range_v1" "vigil" {
  metadata {
    name      = "vigil-defaults"
    namespace = kubernetes_namespace_v1.vigil.metadata[0].name
  }

  spec {
    limit {
      type = "Container"

      default = {
        cpu    = "500m"
        memory = "512Mi"
      }

      default_request = {
        cpu    = "50m"
        memory = "128Mi"
      }
    }
  }
}

# Credentials, generated rather than typed. `random_password` keeps its value in state, so
# the state file is sensitive and must not be committed -- the .gitignore beside this file
# covers it, and docs/DEPLOYMENT.md repeats the warning where someone will actually read it.
#
# The alternative, a var per password, moves the secret into a tfvars file or into a shell
# history, which is worse. The alternative to *both* is an external secret manager, which is
# the right answer for a real deployment and is noted in ADR-047 as the upgrade.
resource "random_password" "postgres" {
  length  = 32
  special = false
}

resource "random_password" "clickhouse" {
  length  = 32
  special = false
}

resource "random_password" "minio" {
  length  = 32
  special = false
}

resource "random_id" "kafka_cluster" {
  byte_length = 16
}

resource "kubernetes_secret_v1" "vigil" {
  metadata {
    name      = "vigil-secrets"
    namespace = kubernetes_namespace_v1.vigil.metadata[0].name

    labels = {
      "app.kubernetes.io/part-of"    = "vigil"
      "app.kubernetes.io/managed-by" = "terraform"
    }
  }

  type = "Opaque"

  data = {
    POSTGRES_USER       = var.postgres_user
    POSTGRES_PASSWORD   = random_password.postgres.result
    CLICKHOUSE_USER     = var.clickhouse_user
    CLICKHOUSE_PASSWORD = random_password.clickhouse.result
    MINIO_ROOT_USER     = var.minio_user
    MINIO_ROOT_PASSWORD = random_password.minio.result
    KAFKA_CLUSTER_ID    = random_id.kafka_cluster.b64_url
  }
}
