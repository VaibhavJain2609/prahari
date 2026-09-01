locals {
  name = "prahari-${var.district}"

  # Node count derives from the MEASURED streams-per-GPU figure, not a guess.
  # This is the arithmetic the 80,000-camera claim rests on, expressed as code
  # so it cannot drift from the deck.
  gpu_node_count = max(1, ceil(var.camera_count / var.streams_per_gpu))

  tags = merge(var.tags, {
    Project   = "prahari"
    District  = var.district
    ManagedBy = "terraform"
  })
}

# --- network -----------------------------------------------------------------
# Edge and central planes are segmented. A compromised edge node must not be
# able to reach the central metadata plane laterally — this is a threat-model
# requirement, not a convention.

resource "aws_vpc" "district" {
  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true
  tags                 = merge(local.tags, { Name = "${local.name}-vpc" })
}

resource "aws_subnet" "edge" {
  vpc_id            = aws_vpc.district.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, 1)
  availability_zone = "${var.region}a"
  tags              = merge(local.tags, { Name = "${local.name}-edge", Plane = "edge" })
}

resource "aws_security_group" "edge" {
  name        = "${local.name}-edge"
  description = "Edge inference nodes. Video stays inside this boundary."
  vpc_id      = aws_vpc.district.id

  # Egress to the central metadata plane only. Detections and alerts leave;
  # pixels do not. Evidence retrieval is a separate, audited, pull-based path.
  egress {
    description = "Metadata plane (gRPC)"
    from_port   = 9001
    to_port     = 9001
    protocol    = "tcp"
    cidr_blocks = ["10.255.0.0/16"] # central plane
  }

  egress {
    description = "HTTPS for image pulls and model weights"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.tags, { Name = "${local.name}-edge-sg" })
}

# --- edge compute ------------------------------------------------------------

resource "aws_key_pair" "node" {
  key_name   = "${local.name}-node"
  public_key = var.ssh_public_key
  tags       = local.tags
}

resource "aws_instance" "gpu_node" {
  count = local.gpu_node_count

  ami                    = data.aws_ami.gpu.id
  instance_type          = var.gpu_instance_type
  subnet_id              = aws_subnet.edge.id
  vpc_security_group_ids = [aws_security_group.edge.id]
  key_name               = aws_key_pair.node.key_name

  # k3s + NVIDIA device plugin. Same bootstrap the local k3d cluster mirrors,
  # so the Helm charts run unchanged in both places.
  user_data = templatefile("${path.module}/cloud-init.sh.tftpl", {
    k3s_version = var.k3s_version
    node_index  = count.index
    district    = var.district
  })

  root_block_device {
    volume_size = 100
    encrypted   = true # encryption at rest is not optional for this workload
  }

  tags = merge(local.tags, {
    Name  = "${local.name}-gpu-${count.index}"
    Plane = "edge"
  })
}

data "aws_ami" "gpu" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 22.04)*"]
  }
}
