variable "home_region" {
  type    = string
  default = "me-central-1"
}

variable "allowed_regions" {
  description = "Regions where regional services may be used (UAE data residency)."
  type        = list(string)
  default     = ["me-central-1"]
}

variable "dr_copy_region" {
  description = "Extra region allowed ONLY when a client has consented to encrypted DR copies abroad. null = none."
  type        = string
  default     = null
}

variable "bedrock_regions" {
  description = "Regions Bedrock cross-region inference may route to (prompts leave the UAE - client consent). Empty = Bedrock limited to allowed_regions."
  type        = list(string)
  default     = []
}

variable "target_ids" {
  description = "OU ids (ou-...) or account ids the SCP is attached to: the workloads OU holding staging, pack-prod, auto-prod."
  type        = list(string)
}

variable "exempt_role_arn_patterns" {
  description = "Break-glass roles not bound by the region lock (keep this list tiny and audited)."
  type        = list(string)
  default     = ["arn:aws:iam::*:role/KairoxBreakGlass"]
}
