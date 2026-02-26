terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
  required_version = ">= 1.6"
}

provider "aws" {
  region = var.aws_region
}

locals {
  name   = "${var.project}-${var.env}"
  prefix = var.project
}

# ── VPC (default VPC for simplicity) ─────────────────────
data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

# ── ECR Repositories ──────────────────────────────────────
resource "aws_ecr_repository" "repos" {
  for_each             = toset(["wa-service", "processor", "bot", "analytics"])
  name                 = "${local.prefix}-${each.key}"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

# ── S3 Bucket (media) ─────────────────────────────────────
resource "aws_s3_bucket" "media" {
  bucket = "${local.prefix}-media-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_public_access_block" "media" {
  bucket = aws_s3_bucket.media.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

data "aws_caller_identity" "current" {}

# ── EFS (WhatsApp session persistence) ───────────────────
resource "aws_efs_file_system" "wa_sessions" {
  tags = { Name = "${local.name}-wa-sessions" }
}

resource "aws_efs_mount_target" "wa" {
  for_each        = toset(data.aws_subnets.default.ids)
  file_system_id  = aws_efs_file_system.wa_sessions.id
  subnet_id       = each.value
  security_groups = [aws_security_group.efs.id]
}

# ── Security Groups ───────────────────────────────────────
resource "aws_security_group" "efs" {
  name   = "${local.name}-efs"
  vpc_id = data.aws_vpc.default.id

  ingress {
    from_port   = 2049
    to_port     = 2049
    protocol    = "tcp"
    cidr_blocks = [data.aws_vpc.default.cidr_block]
  }
}

resource "aws_security_group" "services" {
  name   = "${local.name}-services"
  vpc_id = data.aws_vpc.default.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    from_port   = 3000
    to_port     = 3000
    protocol    = "tcp"
    cidr_blocks = [data.aws_vpc.default.cidr_block]
  }

  ingress {
    from_port   = 8000
    to_port     = 8001
    protocol    = "tcp"
    cidr_blocks = [data.aws_vpc.default.cidr_block]
  }
}

# ── ElastiCache Redis ─────────────────────────────────────
resource "aws_elasticache_subnet_group" "redis" {
  name       = "${local.name}-redis"
  subnet_ids = data.aws_subnets.default.ids
}

resource "aws_elasticache_cluster" "redis" {
  cluster_id           = "${local.prefix}-redis"
  engine               = "redis"
  node_type            = "cache.t3.micro"
  num_cache_nodes      = 1
  parameter_group_name = "default.redis7"
  subnet_group_name    = aws_elasticache_subnet_group.redis.name
  port                 = 6379
}

# ── RDS PostgreSQL ────────────────────────────────────────
resource "aws_db_subnet_group" "pg" {
  name       = "${local.name}-pg"
  subnet_ids = data.aws_subnets.default.ids
}

resource "aws_db_instance" "pg" {
  identifier             = "${local.prefix}-db"
  engine                 = "postgres"
  engine_version         = "16"
  instance_class         = "db.t3.micro"
  allocated_storage      = 20
  db_name                = "bridge"
  username               = "bridge"
  password               = var.db_password
  db_subnet_group_name   = aws_db_subnet_group.pg.name
  vpc_security_group_ids = [aws_security_group.services.id]
  skip_final_snapshot    = true
  publicly_accessible    = false
}

# ── ECS Cluster ───────────────────────────────────────────
resource "aws_ecs_cluster" "main" {
  name = local.name
}

# ── IAM Role for ECS tasks ────────────────────────────────
resource "aws_iam_role" "ecs_task" {
  name = "${local.name}-ecs-task"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_s3" {
  role       = aws_iam_role.ecs_task.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonS3FullAccess"
}

resource "aws_iam_role_policy_attachment" "ecs_ecr" {
  role       = aws_iam_role.ecs_task.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}

# ── Outputs ───────────────────────────────────────────────
output "ecr_urls" {
  value = { for k, v in aws_ecr_repository.repos : k => v.repository_url }
}

output "redis_endpoint" {
  value = aws_elasticache_cluster.redis.cache_nodes[0].address
}

output "db_endpoint" {
  value = aws_db_instance.pg.endpoint
}

output "s3_bucket" {
  value = aws_s3_bucket.media.bucket
}

output "efs_id" {
  value = aws_efs_file_system.wa_sessions.id
}
