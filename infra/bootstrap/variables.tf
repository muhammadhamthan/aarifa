variable "account_id" {
  type = string
}

variable "region" {
  type    = string
  default = "me-central-1"
}

variable "env" {
  description = "management | staging | prod"
  type        = string
}

variable "company" {
  description = "pack | auto | shared | kairox"
  type        = string
}

variable "cost_center" {
  type = string
}

variable "bucket_name" {
  description = "e.g. kx-tfstate-pack-prod-<account_id>"
  type        = string
}

variable "noncurrent_version_days" {
  type    = number
  default = 90
}
