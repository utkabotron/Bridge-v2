# ECS Task Definitions and Services for Bridge v2
# Each service maps directly to a Docker image in ECR.

locals {
  account_id = data.aws_caller_identity.current.account_id
  db_url     = "postgresql://bridge:${var.db_password}@${aws_db_instance.pg.address}:5432/bridge"
  redis_host = aws_elasticache_cluster.redis.cache_nodes[0].address
}

# ── CloudWatch Log Groups ─────────────────────────────────
resource "aws_cloudwatch_log_group" "services" {
  for_each          = toset(["wa-service", "processor", "bot", "analytics"])
  name              = "/ecs/${local.name}/${each.key}"
  retention_in_days = 14
}

# ── Task Execution Role ───────────────────────────────────
resource "aws_iam_role" "ecs_exec" {
  name = "${local.name}-ecs-exec"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_exec_policy" {
  role       = aws_iam_role.ecs_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# ── wa-service Task ───────────────────────────────────────
resource "aws_ecs_task_definition" "wa_service" {
  family                   = "${local.name}-wa-service"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "1024"
  memory                   = "2048"
  task_role_arn            = aws_iam_role.ecs_task.arn
  execution_role_arn       = aws_iam_role.ecs_exec.arn

  volume {
    name = "wa-sessions"
    efs_volume_configuration {
      file_system_id = aws_efs_file_system.wa_sessions.id
      root_directory = "/"
    }
  }

  container_definitions = jsonencode([{
    name  = "wa-service"
    image = "${aws_ecr_repository.repos["wa-service"].repository_url}:latest"
    portMappings = [{ containerPort = 3000 }]

    environment = [
      { name = "REDIS_HOST", value = local.redis_host },
      { name = "REDIS_PORT", value = "6379" },
      { name = "S3_BUCKET",  value = aws_s3_bucket.media.bucket },
      { name = "AWS_REGION", value = var.aws_region },
    ]

    mountPoints = [{
      sourceVolume  = "wa-sessions"
      containerPath = "/app/.wwebjs_auth"
      readOnly      = false
    }]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = "/ecs/${local.name}/wa-service"
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "wa"
      }
    }
  }])
}

resource "aws_ecs_service" "wa_service" {
  name            = "wa-service"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.wa_service.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  # wa-service must NOT do rolling deploy — sessions in EFS
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  network_configuration {
    subnets          = data.aws_subnets.default.ids
    security_groups  = [aws_security_group.services.id]
    assign_public_ip = true
  }

  # Cloud Map: bot reaches wa-service at http://wa-service.bridge-v2-prod.local:3000
  service_registries {
    registry_arn = aws_service_discovery_service.wa_service.arn
  }
}

# ── processor Task ────────────────────────────────────────
resource "aws_ecs_task_definition" "processor" {
  family                   = "${local.name}-processor"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "512"
  memory                   = "512"
  task_role_arn            = aws_iam_role.ecs_task.arn
  execution_role_arn       = aws_iam_role.ecs_exec.arn

  container_definitions = jsonencode([{
    name  = "processor"
    image = "${aws_ecr_repository.repos["processor"].repository_url}:latest"
    portMappings = [{ containerPort = 8000 }]

    environment = [
      { name = "REDIS_HOST",              value = local.redis_host },
      { name = "DATABASE_URL",            value = local.db_url },
      { name = "OPENAI_API_KEY",          value = var.openai_api_key },
      { name = "LANGCHAIN_TRACING_V2",    value = "true" },
      { name = "LANGCHAIN_API_KEY",       value = var.langchain_api_key },
      { name = "LANGCHAIN_PROJECT",       value = "bridge-v2" },
      { name = "TARGET_LANGUAGE",         value = "Hebrew" },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = "/ecs/${local.name}/processor"
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "proc"
      }
    }
  }])
}

resource "aws_ecs_service" "processor" {
  name            = "processor"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.processor.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = data.aws_subnets.default.ids
    security_groups  = [aws_security_group.services.id]
    assign_public_ip = true
  }

  service_registries {
    registry_arn = aws_service_discovery_service.processor.arn
  }
}

# ── bot Task ──────────────────────────────────────────────
resource "aws_ecs_task_definition" "bot" {
  family                   = "${local.name}-bot"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "256"
  task_role_arn            = aws_iam_role.ecs_task.arn
  execution_role_arn       = aws_iam_role.ecs_exec.arn

  container_definitions = jsonencode([{
    name  = "bot"
    image = "${aws_ecr_repository.repos["bot"].repository_url}:latest"
    portMappings = [{ containerPort = 8001 }]

    environment = [
      { name = "TELEGRAM_BOT_TOKEN", value = var.telegram_bot_token },
      { name = "REDIS_HOST",         value = local.redis_host },
      { name = "DATABASE_URL",       value = local.db_url },
      { name = "WA_SERVICE_URL",     value = "http://wa-service.${local.name}.local:3000" },
      { name = "ADMIN_TG_IDS",       value = var.admin_tg_ids },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = "/ecs/${local.name}/bot"
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "bot"
      }
    }
  }])
}

resource "aws_ecs_service" "bot" {
  name            = "bot"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.bot.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = data.aws_subnets.default.ids
    security_groups  = [aws_security_group.services.id]
    assign_public_ip = true
  }
}

# ── analytics Task ────────────────────────────────────────
resource "aws_ecs_task_definition" "analytics" {
  family                   = "${local.name}-analytics"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "256"
  task_role_arn            = aws_iam_role.ecs_task.arn
  execution_role_arn       = aws_iam_role.ecs_exec.arn

  container_definitions = jsonencode([{
    name  = "analytics"
    image = "${aws_ecr_repository.repos["analytics"].repository_url}:latest"

    environment = [
      { name = "DATABASE_URL",        value = local.db_url },
      { name = "BIGQUERY_PROJECT",    value = "" },
      { name = "BIGQUERY_DATASET",    value = "bridge_analytics" },
      { name = "WA_SERVICE_URL",      value = "http://wa-service:3000" },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = "/ecs/${local.name}/analytics"
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "analytics"
      }
    }
  }])
}

resource "aws_ecs_service" "analytics" {
  name            = "analytics"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.analytics.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = data.aws_subnets.default.ids
    security_groups  = [aws_security_group.services.id]
    assign_public_ip = true
  }
}
