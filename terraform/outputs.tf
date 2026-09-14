output "namespace" {
  description = "Namespace the workloads were created in."
  value       = kubernetes_namespace_v1.vigil.metadata[0].name
}

output "secret_name" {
  description = "Name of the Secret the manifests reference by envFrom."
  value       = kubernetes_secret_v1.vigil.metadata[0].name
}

output "next_step" {
  description = "What to run once terraform apply has finished."
  value       = "kubectl apply -k k8s/   # the Secret and namespace already exist"
}

# The generated passwords are deliberately NOT outputs, not even sensitive ones. An output
# invites `terraform output -raw`, which puts a live credential in a shell history. Read them
# from the cluster when they are genuinely needed:
#
#   kubectl -n vigil get secret vigil-secrets -o jsonpath='{.data.POSTGRES_PASSWORD}' | base64 -d
