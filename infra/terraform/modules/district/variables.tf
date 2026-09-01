# The district module represents ONE district's edge deployment.
#
# Statewide rollout is `terraform apply -var district=Rajkot`, repeated across
# 34 districts. That is the point of this module: it turns "how would you deploy
# to 34 districts?" from a paragraph in a deck into something runnable.
#
# INVARIANT: nothing district-specific is hardcoded in the module body. If a
# value would differ between Rajkot and Kutch, it is a variable.

variable "district" {
  description = "District name. Used for naming, tagging and cost attribution."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,30}$", var.district))
    error_message = "District must be lowercase alphanumeric with hyphens (e.g. rajkot, banaskantha)."
  }
}

variable "region" {
  description = "Cloud region. Edge compute should sit close to the district's cameras to keep video off the state backbone."
  type        = string
}

variable "camera_count" {
  description = <<-EOT
    Cameras this district will onboard. Drives GPU node sizing.

    Sizing derives from the MEASURED streams-per-GPU figure in
    docs/SCALE-80K.md — not an estimate. If that measurement changes, this
    module's arithmetic changes with it.
  EOT
  type        = number

  validation {
    condition     = var.camera_count > 0 && var.camera_count <= 10000
    error_message = "camera_count must be between 1 and 10000 for a single district."
  }
}

variable "streams_per_gpu" {
  description = <<-EOT
    Measured camera streams a single GPU sustains at the target sampling rate.

    Working estimate is 50 (L4 class, yolov8s, 4 fps sampled, OCR gated on
    detections). Replace with the value measured on Day 4 and recorded in
    infra/loadtest/. A number we cannot reproduce on demand does not ship.
  EOT
  type        = number
  default     = 50
}

variable "gpu_instance_type" {
  description = "GPU instance type for inference workers."
  type        = string
  default     = "g6.xlarge" # L4, 24GB
}

variable "vpc_cidr" {
  description = "CIDR for the district VPC. Edge and central planes are segmented; a compromised edge node must not reach the central metadata plane laterally."
  type        = string
  default     = "10.0.0.0/16"
}

variable "k3s_version" {
  description = "Pinned k3s version. Never floating — a demo that breaks because an upstream tag moved is an unrecoverable loss."
  type        = string
  default     = "v1.31.4+k3s1"
}

variable "ssh_public_key" {
  description = "Public key for node access. The private half never enters Terraform state."
  type        = string
}

variable "tags" {
  description = "Additional tags merged into every resource."
  type        = map(string)
  default     = {}
}
