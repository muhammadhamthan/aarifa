variable "name" {
  description = "Name prefix, e.g. kx-pack-prod."
  type        = string
}

variable "cidr_block" {
  description = "VPC CIDR. Use a different /16 per account so VPCs can be peered later."
  type        = string
  default     = "10.40.0.0/16"
}

variable "az_ids" {
  description = "Availability Zone IDs (not names: names map to different zones per account). me-central-1a/1c = mec1-az1/mec1-az3 in the Kairox accounts; verify with `aws ec2 describe-availability-zones`."
  type        = list(string)
  default     = ["mec1-az1", "mec1-az3"]

  validation {
    condition     = length(var.az_ids) >= 2
    error_message = "At least two AZs are required (ALB and RDS subnet groups need two)."
  }
}

variable "single_nat_gateway" {
  description = "One NAT gateway for all AZs (staging, saves ~$40/month) instead of one per AZ (prod)."
  type        = bool
  default     = false
}

variable "interface_endpoints" {
  description = "Interface VPC endpoints to create (each ~$9/month/AZ). Empty = traffic to AWS APIs goes through NAT."
  type        = list(string)
  default     = []
}

variable "flow_log_retention_days" {
  type    = number
  default = 30
}

variable "kms_key_arn" {
  description = "CMK for the flow-log log group."
  type        = string
}

variable "tags" {
  type    = map(string)
  default = {}
}
