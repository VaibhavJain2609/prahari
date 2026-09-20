output "district" {
  description = "District this deployment serves."
  value       = var.district
}

output "gpu_node_count" {
  description = <<-EOT
    GPU nodes provisioned, derived as ceil(camera_count / streams_per_gpu).

    Summed across all 34 district instantiations, this is the statewide GPU
    figure cited in docs/SCALE-80K.md.
  EOT
  value       = local.gpu_node_count
}

output "cameras_per_node" {
  description = "Effective cameras per GPU node at this district's size."
  value       = ceil(var.camera_count / local.gpu_node_count)
}

output "edge_node_ips" {
  description = "Private IPs of the edge inference nodes. Index 0 is the k3s server."
  value       = aws_instance.gpu_node[*].private_ip
}

output "edge_node_public_ips" {
  description = "Public IPs of the edge inference nodes. Index 0 is the k3s server — SSH and kubeconfig fetches target it."
  value       = aws_instance.gpu_node[*].public_ip
}

output "ssh_hint" {
  description = "SSH into the k3s server node (index 0). The AMI's user is 'ubuntu'."
  value       = "ssh -i <private-key.pem> ubuntu@${aws_instance.gpu_node[0].public_ip}"
}

output "kubeconfig_hint" {
  description = "Fetch the kubeconfig from the server, then point its server: field at the node's public IP (advertised as a TLS SAN at bootstrap)."
  value       = "scp -i <private-key.pem> ubuntu@${aws_instance.gpu_node[0].public_ip}:/etc/rancher/k3s/k3s.yaml ./k3s-${var.district}.yaml"
}

output "next_steps" {
  description = "What to do after terraform apply returns."
  value       = <<-EOT
    1. Wait for bootstrap:  ssh ubuntu@${aws_instance.gpu_node[0].public_ip} \
         'sudo test -f /var/log/prahari-bootstrap.done && cat /var/log/prahari-bootstrap.done'
    2. Fetch kubeconfig:    scp ubuntu@${aws_instance.gpu_node[0].public_ip}:/etc/rancher/k3s/k3s.yaml ./k3s-${var.district}.yaml
       (k3s.yaml is mode 600 root — use sudo: 'ssh ... sudo cat /etc/rancher/k3s/k3s.yaml > ./k3s-${var.district}.yaml')
    3. Point it at the public IP: edit server: to https://${aws_instance.gpu_node[0].public_ip}:6443
       (the cert already carries that IP as a TLS SAN — no kube-system hacks needed)
    4. Deploy:  KUBECONFIG=./k3s-${var.district}.yaml helm upgrade --install prahari infra/helm/prahari -f infra/helm/prahari/values-gpu.yaml
  EOT
}

output "vpc_id" {
  description = "District VPC. Segmented from the central metadata plane."
  value       = aws_vpc.district.id
}
