# Demo environment — one district, real GPU, applied on Day 4.
#
# This root config exists to prove the district module composes. Statewide
# rollout is this same block repeated with different -var values; nothing in
# the module changes.

terraform {
  required_version = ">= 1.7"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.region
}

variable "region" {
  type    = string
  default = "ap-south-1" # Mumbai — closest to Gujarat, keeps video off the backbone
}

variable "ssh_public_key" {
  description = "Public half only. The private key never enters state."
  type        = string
}

module "rajkot" {
  source = "../../modules/district"

  district       = "rajkot"
  region         = var.region
  camera_count   = 50 # the ~50 government feeds available to the hackathon
  ssh_public_key = var.ssh_public_key

  tags = {
    Environment = "demo"
    Event       = "gujarat-hackathon-2026"
  }
}

output "gpu_node_count" {
  value = module.rajkot.gpu_node_count
}

output "edge_node_ips" {
  value = module.rajkot.edge_node_ips
}
