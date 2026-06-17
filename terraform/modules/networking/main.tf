# =============================================================================
# modules/networking/main.tf
#
# Creates the security groups and VPC endpoints that let Fargate tasks in
# private subnets reach AWS APIs without a NAT gateway.
#
# Gateway endpoints (S3, DynamoDB) — free.
# Interface endpoints (ECR API, ECR DKR, CloudWatch Logs, Step Functions)
# — ~$7/ep/AZ/month but eliminate NAT data-processing charges that exceed
# this at any meaningful sensor scale.
# =============================================================================

data "aws_vpc" "target" {
  id = var.vpc_id
}

# Prefer route tables tagged Tier=private; fall back to the main route table.
data "aws_route_tables" "private" {
  vpc_id = var.vpc_id
  filter {
    name   = "tag:Tier"
    values = ["private"]
  }
}

data "aws_route_table" "main" {
  vpc_id = var.vpc_id
  filter {
    name   = "association.main"
    values = ["true"]
  }
}

locals {
  # HCL ternary must stay on one expression — no backslash continuation.
  route_table_ids = (
    length(data.aws_route_tables.private.ids) > 0
    ? data.aws_route_tables.private.ids
    : [data.aws_route_table.main.id]
  )
}

# ── Security Group: VPC interface endpoints ───────────────────────────────────
# Declared first so the tasks SG can reference it without a forward-reference
# error. The inbound rule allowing task traffic is added as a separate
# aws_security_group_rule to break the circular dependency.
resource "aws_security_group" "vpc_endpoints" {
  name        = "${var.name_prefix}-vpc-endpoints"
  description = "Allow HTTPS inbound from Fargate tasks to VPC interface endpoints"
  vpc_id      = var.vpc_id

  egress {
    description = "Allow all outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-vpc-endpoints" })

  lifecycle {
    create_before_destroy = true
  }
}

# ── Security Group: Fargate tasks ─────────────────────────────────────────────
resource "aws_security_group" "tasks" {
  name        = "${var.name_prefix}-tasks"
  description = "Fargate gap-filling tasks — egress to VPC endpoints only"
  vpc_id      = var.vpc_id

  # No ingress — tasks never receive inbound connections.
  # Egress to gateway endpoints is route-based (not SG-based); allow via CIDR.
  egress {
    description = "HTTPS to S3/DDB gateway endpoints (route-table based)"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [data.aws_vpc.target.cidr_block]
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-tasks" })

  lifecycle {
    create_before_destroy = true
  }
}

# Separate rules to avoid circular reference between the two security groups.
resource "aws_security_group_rule" "tasks_to_endpoints_egress" {
  type                     = "egress"
  description              = "HTTPS to VPC interface endpoint ENIs"
  from_port                = 443
  to_port                  = 443
  protocol                 = "tcp"
  security_group_id        = aws_security_group.tasks.id
  source_security_group_id = aws_security_group.vpc_endpoints.id
}

resource "aws_security_group_rule" "endpoints_from_tasks_ingress" {
  type                     = "ingress"
  description              = "HTTPS from Fargate tasks"
  from_port                = 443
  to_port                  = 443
  protocol                 = "tcp"
  security_group_id        = aws_security_group.vpc_endpoints.id
  source_security_group_id = aws_security_group.tasks.id
}

# ── Gateway Endpoints (free) ──────────────────────────────────────────────────
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = var.vpc_id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = local.route_table_ids
  tags              = merge(var.tags, { Name = "${var.name_prefix}-ep-s3" })
}

resource "aws_vpc_endpoint" "dynamodb" {
  vpc_id            = var.vpc_id
  service_name      = "com.amazonaws.${var.aws_region}.dynamodb"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = local.route_table_ids
  tags              = merge(var.tags, { Name = "${var.name_prefix}-ep-dynamodb" })
}

# ── Interface Endpoints ───────────────────────────────────────────────────────
# ecr.api  — image manifest resolution
# ecr.dkr  — image layer download
# logs     — CloudWatch structured log shipping
# states   — Step Functions .sync ECS callback
locals {
  interface_services = toset(["ecr.api", "ecr.dkr", "logs", "states"])
}

resource "aws_vpc_endpoint" "interfaces" {
  for_each = local.interface_services

  vpc_id              = var.vpc_id
  service_name        = "com.amazonaws.${var.aws_region}.${each.value}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = var.private_subnet_ids
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-ep-${replace(each.value, ".", "-")}"
  })
}
