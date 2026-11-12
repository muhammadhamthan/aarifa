# VPC with three subnet tiers per AZ:
#   public  - ALB + NAT gateways
#   private - ECS tasks (egress via NAT to Meta, Twilio, LiveKit, Anthropic ...)
#   data    - RDS + ElastiCache (no route to the internet)

data "aws_region" "current" {}

locals {
  az_count = length(var.az_ids)
  nat_keys = var.single_nat_gateway ? [var.az_ids[0]] : var.az_ids
  # /20 per subnet: public 0-1, private 4-5, data 8-9
  public_cidrs  = { for i, az in var.az_ids : az => cidrsubnet(var.cidr_block, 4, i) }
  private_cidrs = { for i, az in var.az_ids : az => cidrsubnet(var.cidr_block, 4, i + 4) }
  data_cidrs    = { for i, az in var.az_ids : az => cidrsubnet(var.cidr_block, 4, i + 8) }
}

resource "aws_vpc" "this" {
  cidr_block           = var.cidr_block
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = merge(var.tags, { Name = var.name })
}

# Lock down the default security group (no rules = no traffic).
resource "aws_default_security_group" "this" {
  vpc_id = aws_vpc.this.id
  tags   = merge(var.tags, { Name = "${var.name}-default-deny" })
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = merge(var.tags, { Name = var.name })
}

resource "aws_subnet" "public" {
  for_each             = local.public_cidrs
  vpc_id               = aws_vpc.this.id
  cidr_block           = each.value
  availability_zone_id = each.key
  # ALB and NAT get their own addresses; tasks never get public IPs.
  map_public_ip_on_launch = false
  tags                    = merge(var.tags, { Name = "${var.name}-public-${each.key}", Tier = "public" })
}

resource "aws_subnet" "private" {
  for_each             = local.private_cidrs
  vpc_id               = aws_vpc.this.id
  cidr_block           = each.value
  availability_zone_id = each.key
  tags                 = merge(var.tags, { Name = "${var.name}-private-${each.key}", Tier = "private" })
}

resource "aws_subnet" "data" {
  for_each             = local.data_cidrs
  vpc_id               = aws_vpc.this.id
  cidr_block           = each.value
  availability_zone_id = each.key
  tags                 = merge(var.tags, { Name = "${var.name}-data-${each.key}", Tier = "data" })
}

resource "aws_eip" "nat" {
  # checkov:skip=CKV2_AWS_19: attached to the NAT gateway
  for_each = toset(local.nat_keys)
  domain   = "vpc"
  tags     = merge(var.tags, { Name = "${var.name}-nat-${each.key}" })
}

resource "aws_nat_gateway" "this" {
  for_each      = toset(local.nat_keys)
  allocation_id = aws_eip.nat[each.key].id
  subnet_id     = aws_subnet.public[each.key].id
  tags          = merge(var.tags, { Name = "${var.name}-nat-${each.key}" })
  depends_on    = [aws_internet_gateway.this]
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  tags   = merge(var.tags, { Name = "${var.name}-public" })
}

resource "aws_route" "public_internet" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this.id
}

resource "aws_route_table_association" "public" {
  for_each       = aws_subnet.public
  subnet_id      = each.value.id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table" "private" {
  for_each = toset(var.az_ids)
  vpc_id   = aws_vpc.this.id
  tags     = merge(var.tags, { Name = "${var.name}-private-${each.key}" })
}

resource "aws_route" "private_nat" {
  for_each               = toset(var.az_ids)
  route_table_id         = aws_route_table.private[each.key].id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.this[var.single_nat_gateway ? var.az_ids[0] : each.key].id
}

resource "aws_route_table_association" "private" {
  for_each       = aws_subnet.private
  subnet_id      = each.value.id
  route_table_id = aws_route_table.private[each.key].id
}

# Data tier: local routes only.
resource "aws_route_table" "data" {
  vpc_id = aws_vpc.this.id
  tags   = merge(var.tags, { Name = "${var.name}-data" })
}

resource "aws_route_table_association" "data" {
  for_each       = aws_subnet.data
  subnet_id      = each.value.id
  route_table_id = aws_route_table.data.id
}

# S3 gateway endpoint is free and keeps S3 traffic (media, audio, quotes) off the NAT.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${data.aws_region.current.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [for rt in aws_route_table.private : rt.id]
  tags              = merge(var.tags, { Name = "${var.name}-s3" })
}

resource "aws_security_group" "endpoints" {
  count       = length(var.interface_endpoints) > 0 ? 1 : 0
  name        = "${var.name}-vpce"
  description = "Interface VPC endpoints: HTTPS from inside the VPC"
  vpc_id      = aws_vpc.this.id
  tags        = var.tags
}

resource "aws_vpc_security_group_ingress_rule" "endpoints_https" {
  count             = length(var.interface_endpoints) > 0 ? 1 : 0
  security_group_id = aws_security_group.endpoints[0].id
  description       = "HTTPS from the VPC"
  cidr_ipv4         = var.cidr_block
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_endpoint" "interface" {
  for_each            = toset(var.interface_endpoints)
  vpc_id              = aws_vpc.this.id
  service_name        = "com.amazonaws.${data.aws_region.current.region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  private_dns_enabled = true
  subnet_ids          = [for s in aws_subnet.private : s.id]
  security_group_ids  = [aws_security_group.endpoints[0].id]
  tags                = merge(var.tags, { Name = "${var.name}-${each.key}" })
}

# One security group shared by every application task (api, worker, beat, voice, cms, embeddings,
# phoenix, otel-collector, migrate).
# RDS and ElastiCache allow ingress from it; the ALB module adds ingress from the ALB.
resource "aws_security_group" "app" {
  # checkov:skip=CKV2_AWS_5: attached to ECS services/tasks in other modules
  name        = "${var.name}-app"
  description = "ECS application tasks"
  vpc_id      = aws_vpc.this.id
  tags        = merge(var.tags, { Name = "${var.name}-app" })
}

resource "aws_vpc_security_group_egress_rule" "app_all" {
  security_group_id = aws_security_group.app.id
  description       = "Outbound to AWS APIs, RDS, Redis and third-party APIs (Meta, Telnyx, LiveKit, Claude ...)"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

# Private services inside the app group (11 Nov): the Qwen3 embedding server, Arize Phoenix (UI + OTLP)
# and the OpenTelemetry collector are reached only by other app tasks, never through the ALB.
resource "aws_vpc_security_group_ingress_rule" "app_internal" {
  for_each                     = var.internal_service_ports
  security_group_id            = aws_security_group.app.id
  description                  = "App tasks to the private ${each.key} service"
  referenced_security_group_id = aws_security_group.app.id
  ip_protocol                  = "tcp"
  from_port                    = each.value.from
  to_port                      = each.value.to
}

# ---- VPC flow logs (security monitoring) --------------------------------
resource "aws_cloudwatch_log_group" "flow" {
  # checkov:skip=CKV_AWS_338: 30-day retention decided for operational logs (cost); audit trail is exported to S3
  name              = "/kx/${var.name}/vpc-flow-logs"
  retention_in_days = var.flow_log_retention_days
  kms_key_id        = var.kms_key_arn
  tags              = var.tags
}

data "aws_iam_policy_document" "flow_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["vpc-flow-logs.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "flow" {
  name               = "${var.name}-vpc-flow-logs"
  assume_role_policy = data.aws_iam_policy_document.flow_assume.json
  tags               = var.tags
}

data "aws_iam_policy_document" "flow" {
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
    resources = ["${aws_cloudwatch_log_group.flow.arn}:*"]
  }
}

resource "aws_iam_role_policy" "flow" {
  name   = "write-flow-logs"
  role   = aws_iam_role.flow.id
  policy = data.aws_iam_policy_document.flow.json
}

resource "aws_flow_log" "this" {
  vpc_id          = aws_vpc.this.id
  traffic_type    = "REJECT"
  iam_role_arn    = aws_iam_role.flow.arn
  log_destination = aws_cloudwatch_log_group.flow.arn
  tags            = var.tags
}
