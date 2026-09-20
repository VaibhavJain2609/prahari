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

  # State is local to this env for now — fine for a one-operator demo, wrong
  # for the rollout where several people apply the same modules. No state
  # bucket exists yet; when one is provisioned, uncomment and run
  # `terraform init -migrate-state`:
  #
  # backend "s3" {
  #   bucket         = "prahari-terraform-state"
  #   key            = "envs/demo/terraform.tfstate"
  #   region         = "ap-south-1"
  #   dynamodb_table = "prahari-terraform-locks"
  #   encrypt        = true
  # }
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

variable "ssh_cidr" {
  description = "Operator CIDR allowed SSH (22) and k3s API (6443) access to edge nodes — your egress IP as a /32."
  type        = string
}

module "rajkot" {
  source = "../../modules/district"

  district       = "rajkot"
  region         = var.region
  camera_count   = 50 # the ~50 government feeds available to the hackathon
  ssh_public_key = var.ssh_public_key
  ssh_cidr       = var.ssh_cidr

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

output "edge_node_public_ips" {
  value = module.rajkot.edge_node_public_ips
}

output "ssh_hint" {
  value = module.rajkot.ssh_hint
}

output "kubeconfig_hint" {
  value = module.rajkot.kubeconfig_hint
}

output "next_steps" {
  value = module.rajkot.next_steps
}
