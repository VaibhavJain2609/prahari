locals {
  name = "prahari-${var.district}"

  # Node count derives from the MEASURED streams-per-GPU figure, not a guess.
  # This is the arithmetic the 80,000-camera claim rests on, expressed as code
  # so it cannot drift from the deck.
  gpu_node_count = max(1, ceil(var.camera_count / var.streams_per_gpu))

  edge_subnet_cidr = cidrsubnet(var.vpc_cidr, 8, 1)

  # Node 0 is always the k3s server. It gets a deterministic private IP so
  # agents can be handed K3S_URL at plan time — no SSM/IAM token handoff,
  # no describe-instances lookup, nothing extra to break under time pressure.
  k3s_server_private_ip = cidrhost(local.edge_subnet_cidr, 10)

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

resource "aws_internet_gateway" "district" {
  vpc_id = aws_vpc.district.id
  tags   = merge(local.tags, { Name = "${local.name}-igw" })
}

resource "aws_subnet" "edge" {
  vpc_id            = aws_vpc.district.id
  cidr_block        = local.edge_subnet_cidr
  availability_zone = "${var.region}a"

  # Nodes bootstrap from the internet (get.k3s.io, apt, NVIDIA repos, image
  # pulls) before the cluster exists — they need a public IP at launch, or
  # cloud-init's first curl fails and the node comes up half-built.
  map_public_ip_on_launch = true

  tags = merge(local.tags, { Name = "${local.name}-edge", Plane = "edge" })
}

resource "aws_route_table" "edge" {
  vpc_id = aws_vpc.district.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.district.id
  }

  tags = merge(local.tags, { Name = "${local.name}-edge-rt" })
}

resource "aws_route_table_association" "edge" {
  subnet_id      = aws_subnet.edge.id
  route_table_id = aws_route_table.edge.id
}

resource "aws_security_group" "edge" {
  name        = "${local.name}-edge"
  description = "Edge inference nodes. Video stays inside this boundary."
  vpc_id      = aws_vpc.district.id

  # Admin plane: SSH for operators, 6443 so kubectl can reach the edge cluster
  # during bring-up. Both are pinned to var.ssh_cidr — a GPU node with an
  # open API is a cryptomining target.
  ingress {
    description = "SSH from operator CIDR"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.ssh_cidr]
  }

  ingress {
    description = "k3s API from operator CIDR"
    from_port   = 6443
    to_port     = 6443
    protocol    = "tcp"
    cidr_blocks = [var.ssh_cidr]
  }

  # Intra-cluster: agents reach the server on 6443, flannel VXLAN is 8472/udp,
  # kubelet 10250, metrics 10250 — every node-to-node flow stays inside the
  # group and never touches the admin CIDR.
  ingress {
    description = "All traffic between edge nodes"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    self        = true
  }

  egress {
    description = "All traffic between edge nodes"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    self        = true
  }

  # Egress to the central metadata plane only. Detections and alerts leave;
  # pixels do not. Evidence retrieval is a separate, audited, pull-based path.
  egress {
    description = "Metadata plane (gRPC)"
    from_port   = 9001
    to_port     = 9001
    protocol    = "tcp"
    cidr_blocks = [var.central_plane_cidr]
  }

  # The data plane: MediaMTX pulls upstream camera feeds INBOUND to the edge —
  # RTSP (554 is the registry probe allowlist port; 8554 is the gateway's
  # documented media port) and the WHEP/HLS fallbacks. Without these the feeds
  # never reach the edge and the whole pipeline is alive but empty. Wide CIDR
  # because the gateway address is operator-supplied; scope it down with
  # var.gateway_cidr when the address is known and stable.
  dynamic "egress" {
    for_each = var.gateway_cidr != "" ? [var.gateway_cidr] : ["0.0.0.0/0"]
    content {
      description = "Upstream camera media pull (RTSP TCP)"
      from_port   = 554
      to_port     = 554
      protocol    = "tcp"
      cidr_blocks = [egress.value]
    }
  }

  dynamic "egress" {
    for_each = var.gateway_cidr != "" ? [var.gateway_cidr] : ["0.0.0.0/0"]
    content {
      description = "Upstream camera media pull (gateway RTSP/WHEP)"
      from_port   = 8554
      to_port     = 8889
      protocol    = "tcp"
      cidr_blocks = [egress.value]
    }
  }

  # Bootstrap egress — the remaining rules exist because a node that cannot
  # reach the internet at first boot silently produces a half-built node.
  egress {
    description = "HTTPS for get.k3s.io, image pulls and model weights"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "HTTP for apt — Ubuntu archives are served on port 80"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "DNS (TCP)"
    from_port   = 53
    to_port     = 53
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "DNS (UDP)"
    from_port   = 53
    to_port     = 53
    protocol    = "udp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "NTP — TLS validation fails on a skewed clock"
    from_port   = 123
    to_port     = 123
    protocol    = "udp"
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

# Shared cluster secret: the server bootstraps with it and agents present it
# to join. It sits in Terraform state and user_data like any bootstrap
# credential — rotate by tainting this resource (which recreates the nodes).
resource "random_password" "k3s_token" {
  length  = 32
  special = false # k3s tokens ride in env vars and URLs; keep them shell-safe
}

resource "aws_instance" "gpu_node" {
  count = local.gpu_node_count

  ami                    = data.aws_ami.gpu.id
  instance_type          = var.gpu_instance_type
  subnet_id              = aws_subnet.edge.id
  private_ip             = count.index == 0 ? local.k3s_server_private_ip : null
  vpc_security_group_ids = [aws_security_group.edge.id]
  key_name               = aws_key_pair.node.key_name

  # k3s + NVIDIA device plugin. Same bootstrap the local k3d cluster mirrors,
  # so the Helm charts run unchanged in both places. Node 0 runs `server`;
  # every other node joins it as an agent — one cluster, not N.
  user_data = templatefile("${path.module}/cloud-init.sh.tftpl", {
    k3s_version = var.k3s_version
    node_index  = count.index
    district    = var.district
    is_server   = count.index == 0
    server_ip   = local.k3s_server_private_ip
    k3s_token   = random_password.k3s_token.result
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
