# The Kubernetes provider, pointed at a cluster that already exists.
#
# **No cloud provider is configured, and that is the decision, not an omission** (ADR-047).
# A `google_container_cluster` or an `aws_eks_cluster` block here would be the most
# impressive-looking file in the repository and the least honest: nothing in it has ever been
# applied, neither free tier includes a managed Kubernetes control plane, and an untested
# cluster definition is exactly the kind of artifact that reads as production experience
# while being a guess.
#
# What this targets instead is kind or minikube, which is what docs/DEPLOYMENT.md describes
# and what someone can actually run. Provisioning a managed cluster is a `terraform/cloud/`
# module nobody has written, and the absence is deliberate.

provider "kubernetes" {
  config_path = var.kubeconfig_path

  # Empty string means "current context". Terraform has no conditional provider block, so the
  # empty default is how this stays optional; naming it is strongly preferred and the variable
  # says why.
  config_context = var.kubeconfig_context != "" ? var.kubeconfig_context : null
}
