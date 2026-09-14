variable "namespace" {
  description = "Namespace every vigil workload lands in."
  type        = string
  default     = "vigil"
}

variable "kubeconfig_path" {
  description = "Path to the kubeconfig. The default is what kind and minikube write."
  type        = string
  default     = "~/.kube/config"
}

variable "kubeconfig_context" {
  description = <<-EOT
    Context to use. Empty means whatever is current, which is convenient and is also how a
    deploy lands in the wrong cluster; name it explicitly for anything that matters.
  EOT
  type        = string
  default     = ""
}

variable "postgres_user" {
  description = "Postgres role. The password is generated, not configured."
  type        = string
  default     = "vigil"
}

variable "clickhouse_user" {
  type        = string
  description = "ClickHouse user. The password is generated, not configured."
  default     = "vigil"
}

variable "minio_user" {
  type        = string
  description = "MinIO root user. The password is generated, not configured."
  default     = "vigil"
}

# Quota defaults come from measurement, not from a guess. The compose stack was measured at
# 1.2 GB across Kafka, ClickHouse, MinIO and Postgres; the manifests request roughly 3.4 GB
# in total once the four consumers and two API replicas are counted, and the limits sum to
# about 8 GB. These ceilings sit above the limits with room for one rollout to double a
# Deployment briefly, and below what would let a runaway scale eat a node.
variable "quota_requests_cpu" {
  type    = string
  default = "4"
}

variable "quota_requests_memory" {
  type    = string
  default = "8Gi"
}

variable "quota_limits_cpu" {
  type    = string
  default = "16"
}

variable "quota_limits_memory" {
  type    = string
  default = "16Gi"
}

variable "quota_pods" {
  type    = string
  default = "30"
}

variable "quota_requests_storage" {
  description = "Sum of PersistentVolumeClaim requests. The stores ask for 45Gi between them."
  type        = string
  default     = "60Gi"
}
