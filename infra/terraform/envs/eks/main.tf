# Central plane — EKS in ap-south-1.
#
# The district module is the EDGE: k3s on EC2 GPU nodes near the cameras, where
# the pixels stay. This env is the CENTRAL metadata plane: the whole platform
# chart (registry, match-engine, correlation, bff, web, postgres, redis,
# mediamtx) on managed Kubernetes, fed images by GitHub Actions → ECR so the
# laptop never stores a multi-GB image layer.
#
# The VPC CIDR is 10.255.0.0/16 ON PURPOSE: the district module's
# central_plane_cidr variable defaults to exactly this range ("reserved
# placeholder... wiring it in is a tfvars change"). Applying this env makes the
# placeholder real — point a district's central_plane_cidr here and its
# port-9001 egress rule already describes this cluster.

terraform {
  required_version = ">= 1.7"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Same local-state caveat as envs/demo: fine for one operator, wrong for a
  # shared rollout. Migrate both envs to the S3 backend together when the
  # state bucket exists.
}

provider "aws" {
  region = var.region
}

variable "region" {
  type    = string
  default = "ap-south-1" # Mumbai — closest to Gujarat, keeps video off the backbone
}

variable "cluster_name" {
  type    = string
  default = "prahari-central"
}

variable "kubernetes_version" {
  description = "EKS control plane version. Pinned, never floating — an auto-upgrade mid-demo is an unrecoverable loss."
  type        = string
  default     = "1.35"
}

variable "vpc_cidr" {
  description = <<-EOT
    Central-plane VPC. Fixed at 10.255.0.0/16 to match the district module's
    central_plane_cidr default — a district pointed at this range gets its
    metadata-plane egress rule for free.
  EOT
  type        = string
  default     = "10.255.0.0/16"
}

variable "operator_cidr" {
  description = <<-EOT
    Operator CIDR allowed to reach the public EKS API endpoint (443) — your
    egress IP as a /32. Required with no default, same posture as the district
    module's ssh_cidr: an open control plane is a decision, not a default.
  EOT
  type        = string

  validation {
    condition     = can(cidrhost(var.operator_cidr, 0)) && var.operator_cidr != "0.0.0.0/0"
    error_message = "operator_cidr must be a narrow IPv4 CIDR (e.g. \"203.0.113.10/32\"). 0.0.0.0/0 is rejected — use your own egress IP."
  }
}

variable "node_instance_types" {
  description = "General-purpose node group. CPU-only for now — the GPU pool for profile=gpu is a follow-up node group, not part of this env yet."
  type        = list(string)
  default     = ["t3.large"]
}

variable "node_desired" {
  type    = number
  default = 2
}

variable "node_min" {
  type    = number
  default = 2
}

variable "node_max" {
  type    = number
  default = 4
}

variable "github_repo" {
  description = "owner/name of the GitHub repo allowed to push to ECR via OIDC. The trust is scoped to its main branch."
  type        = string
  default     = "VaibhavJain2609/prahari"
}

variable "admin_principal_arns" {
  description = "Extra IAM principals granted EKSClusterAdminPolicy via access entries. The Terraform caller is already admin via bootstrap_cluster_creator_admin_permissions."
  type        = list(string)
  default     = []
}

variable "tags" {
  type    = map(string)
  default = {}
}

data "aws_caller_identity" "current" {}
data "aws_availability_zones" "available" { state = "available" }

locals {
  name = var.cluster_name
  azs  = slice(data.aws_availability_zones.available.names, 0, 3)

  tags = merge(var.tags, {
    Project   = "prahari"
    Plane     = "central"
    ManagedBy = "terraform"
  })
}

# --- network -----------------------------------------------------------------
# Public subnets only, public IPs on nodes: no NAT gateway, because NAT is a
# fixed ~$32/mo/AZ that buys nothing a demo needs (all outbound is pulls:
# ECR, model weights, the gateway feed — none of it initiates inbound).
# The private-subnet move belongs with the private endpoint cutover.

resource "aws_vpc" "central" {
  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true
  tags                 = merge(local.tags, { Name = "${local.name}-vpc" })
}

resource "aws_internet_gateway" "central" {
  vpc_id = aws_vpc.central.id
  tags   = merge(local.tags, { Name = "${local.name}-igw" })
}

resource "aws_subnet" "public" {
  count = length(local.azs)

  vpc_id                  = aws_vpc.central.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, count.index)
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = true

  tags = merge(local.tags, {
    Name = "${local.name}-public-${local.azs[count.index]}"
    # LB discovery tags: the external cloud-controller-manager picks subnets
    # for internet-facing LBs by this tag — absent it, `type: LoadBalancer`
    # services provision into no subnet and sit Pending forever.
    "kubernetes.io/role/elb"                    = "1"
    "kubernetes.io/cluster/${var.cluster_name}" = "shared"
  })
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.central.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.central.id
  }

  tags = merge(local.tags, { Name = "${local.name}-public-rt" })
}

resource "aws_route_table_association" "public" {
  count          = length(aws_subnet.public)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# --- cluster IAM ---------------------------------------------------------------

resource "aws_iam_role" "cluster" {
  name = "${local.name}-cluster"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "eks.amazonaws.com" }
      Action    = ["sts:AssumeRole", "sts:TagSession"]
    }]
  })

  tags = local.tags
}

resource "aws_iam_role_policy_attachment" "cluster" {
  role       = aws_iam_role.cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

resource "aws_iam_role" "node" {
  name = "${local.name}-node"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = local.tags
}

resource "aws_iam_role_policy_attachment" "node" {
  for_each = toset([
    "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
    "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
    # ReadOnly, not pull-through-cache or broader: nodes only pull the six
    # first-party repos plus public substrate images.
    "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
  ])
  role       = aws_iam_role.node.name
  policy_arn = each.value
}

# EBS CSI controller, authenticated by Pod Identity rather than the EKS OIDC
# provider — one less provider to provision, and the association is an EKS
# API call rather than a ServiceAccount annotation that could drift.
resource "aws_iam_role" "ebs_csi" {
  name = "${local.name}-ebs-csi"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "pods.eks.amazonaws.com" }
      Action    = ["sts:AssumeRole", "sts:TagSession"]
    }]
  })

  tags = local.tags
}

resource "aws_iam_role_policy_attachment" "ebs_csi" {
  role       = aws_iam_role.ebs_csi.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}

# --- cluster -------------------------------------------------------------------

resource "aws_eks_cluster" "central" {
  name     = local.name
  version  = var.kubernetes_version
  role_arn = aws_iam_role.cluster.arn

  vpc_config {
    subnet_ids              = aws_subnet.public[*].id
    endpoint_private_access = true
    endpoint_public_access  = true
    public_access_cidrs     = [var.operator_cidr]
  }

  access_config {
    authentication_mode                         = "API_AND_CONFIG_MAP"
    bootstrap_cluster_creator_admin_permissions = true
  }

  depends_on = [aws_iam_role_policy_attachment.cluster]
  tags       = local.tags
}

resource "aws_eks_access_entry" "admin" {
  for_each      = toset(var.admin_principal_arns)
  cluster_name  = aws_eks_cluster.central.name
  principal_arn = each.value
}

resource "aws_eks_access_policy_association" "admin" {
  for_each      = toset(var.admin_principal_arns)
  cluster_name  = aws_eks_cluster.central.name
  principal_arn = each.value
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
  access_scope { type = "cluster" }
}

resource "aws_eks_node_group" "general" {
  cluster_name    = aws_eks_cluster.central.name
  node_group_name = "general"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = aws_subnet.public[*].id

  instance_types = var.node_instance_types
  capacity_type  = "ON_DEMAND"
  disk_size      = 60 # GB — image layers for six services plus postgres/redis

  scaling_config {
    desired_size = var.node_desired
    min_size     = var.node_min
    max_size     = var.node_max
  }

  update_config { max_unavailable = 1 }

  depends_on = [aws_iam_role_policy_attachment.node]
  tags       = local.tags
}

# --- addons ----------------------------------------------------------------------
# Pinned to each version's default rather than addon_version strings: EKS
# resolves the compatible build for the control-plane version, which is the
# only pinning that survives a cluster upgrade without a tf edit.

resource "aws_eks_addon" "vpc_cni" {
  cluster_name = aws_eks_cluster.central.name
  addon_name   = "vpc-cni"
}

resource "aws_eks_addon" "kube_proxy" {
  cluster_name = aws_eks_cluster.central.name
  addon_name   = "kube-proxy"
}

resource "aws_eks_addon" "pod_identity_agent" {
  cluster_name = aws_eks_cluster.central.name
  addon_name   = "eks-pod-identity-agent"
}

resource "aws_eks_addon" "coredns" {
  cluster_name = aws_eks_cluster.central.name
  addon_name   = "coredns"

  # CoreDNS pods can't schedule until a node exists; creating the addon first
  # leaves it DEGRADED and `terraform apply` reports success on a cluster that
  # resolves nothing.
  depends_on = [aws_eks_node_group.general]
}

resource "aws_eks_pod_identity_association" "ebs_csi" {
  cluster_name    = aws_eks_cluster.central.name
  namespace       = "kube-system"
  service_account = "ebs-csi-controller-sa"
  role_arn        = aws_iam_role.ebs_csi.arn
}

resource "aws_eks_addon" "ebs_csi" {
  cluster_name = aws_eks_cluster.central.name
  addon_name   = "aws-ebs-csi-driver"

  # The chart's gp3 StorageClass provisions through this controller — without
  # the pod-identity association in place first the addon comes up but every
  # PVC pends on credential errors.
  depends_on = [
    aws_eks_addon.pod_identity_agent,
    aws_eks_pod_identity_association.ebs_csi,
    aws_eks_node_group.general,
  ]
}

# --- ECR -------------------------------------------------------------------------
# One repo per first-party image. Names match the chart's image composition
# ({registry}/prahari-<name>:<tag>) exactly — a mismatch here is an
# ImagePullBackOff the chart cannot see.

resource "aws_ecr_repository" "services" {
  for_each = toset([
    "prahari-registry",
    "prahari-inference",
    "prahari-match-engine",
    "prahari-correlation",
    "prahari-bff",
    "prahari-web",
  ])

  name                 = each.value
  image_tag_mutability = "MUTABLE" # `latest` moves on every main push

  image_scanning_configuration { scan_on_push = true }

  tags = local.tags
}

resource "aws_ecr_lifecycle_policy" "services" {
  for_each   = aws_ecr_repository.services
  repository = each.value.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 15 images — history without an unbounded bill"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 15
      }
      action = { type = "expire" }
    }]
  })
}

# --- GitHub Actions OIDC ---------------------------------------------------------
# Keyless push from CI: the workflow assumes this role via
# token.actions.githubusercontent.com, so no AWS access key ever sits in a
# repo secret. Trust is narrowed to this repo's main branch — a PR from a
# fork cannot assume it.

resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
  tags            = local.tags
}

data "aws_iam_policy_document" "gha_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repo}:ref:refs/heads/main"]
    }
  }
}

resource "aws_iam_role" "gha_ecr_push" {
  name               = "${local.name}-gha-ecr-push"
  assume_role_policy = data.aws_iam_policy_document.gha_trust.json
  tags               = local.tags
}

data "aws_iam_policy_document" "gha_ecr_push" {
  statement {
    sid       = "AuthToken"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"] # GetAuthorizationToken is unscoped by design
  }

  statement {
    sid    = "Push"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = [for repo in aws_ecr_repository.services : repo.arn]
  }
}

resource "aws_iam_role_policy" "gha_ecr_push" {
  name   = "ecr-push"
  role   = aws_iam_role.gha_ecr_push.id
  policy = data.aws_iam_policy_document.gha_ecr_push.json
}

# --- outputs ---------------------------------------------------------------------

output "cluster_name" {
  value = aws_eks_cluster.central.name
}

output "cluster_endpoint" {
  value = aws_eks_cluster.central.endpoint
}

output "region" {
  value = var.region
}

output "central_plane_cidr" {
  description = "Feed this to a district's central_plane_cidr — its edge SG then egresses port 9001 to this VPC."
  value       = var.vpc_cidr
}

output "ecr_registry" {
  description = "The value for global.imageRegistry under the eks profile — the cluster's view AND the CI push target."
  value       = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.region}.amazonaws.com"
}

output "ecr_repositories" {
  value = { for k, r in aws_ecr_repository.services : k => r.repository_url }
}

output "github_actions_role_arn" {
  description = "Set as the GitHub repo variable AWS_ECR_PUSH_ROLE_ARN — .github/workflows/images-ecr.yml assumes it."
  value       = aws_iam_role.gha_ecr_push.arn
}

output "next_steps" {
  value = <<-EOT
    1. aws eks update-kubeconfig --region ${var.region} --name ${local.name}
    2. Push the GitHub repo variable AWS_ECR_PUSH_ROLE_ARN = ${aws_iam_role.gha_ecr_push.arn}
       (and AWS_REGION = ${var.region}) so the images-ecr workflow can assume it.
    3. Merge to main — CI builds and pushes all images to ECR.
    4. make eks-secrets   # gateway + internal + bootstrap Secrets
    5. make eks-up        # helm upgrade -f values-eks.yaml, registry injected from this output
  EOT
}
