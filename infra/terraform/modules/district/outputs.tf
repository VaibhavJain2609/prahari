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
  description = "Private IPs of the edge inference nodes."
  value       = aws_instance.gpu_node[*].private_ip
}

output "vpc_id" {
  description = "District VPC. Segmented from the central metadata plane."
  value       = aws_vpc.district.id
}
