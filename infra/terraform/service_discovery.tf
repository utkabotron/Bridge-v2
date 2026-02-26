# AWS Cloud Map — service discovery for inter-service communication
# Allows bot to reach wa-service at http://wa-service.bridge-v2.local:3000

resource "aws_service_discovery_private_dns_namespace" "internal" {
  name = "${local.name}.local"
  vpc  = data.aws_vpc.default.id
}

resource "aws_service_discovery_service" "wa_service" {
  name = "wa-service"

  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.internal.id
    dns_records {
      ttl  = 10
      type = "A"
    }
    routing_policy = "MULTIVALUE"
  }

  health_check_custom_config {
    failure_threshold = 1
  }
}

resource "aws_service_discovery_service" "processor" {
  name = "processor"

  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.internal.id
    dns_records {
      ttl  = 10
      type = "A"
    }
    routing_policy = "MULTIVALUE"
  }

  health_check_custom_config {
    failure_threshold = 1
  }
}

output "internal_namespace" {
  value = aws_service_discovery_private_dns_namespace.internal.name
}
