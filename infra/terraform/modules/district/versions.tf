terraform {
  required_version = ">= 1.7"

  # Backend is intentionally env-local for now: state lives next to the env
  # that applies it (envs/demo). That is fine for a one-operator Day-4 demo
  # and wrong for the 34-district rollout where several people apply the same
  # module — envs/demo/main.tf carries a commented S3 backend block for the
  # day a shared state bucket is provisioned.
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}
