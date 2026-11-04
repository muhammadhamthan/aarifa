terraform {
  required_version = ">= 1.13"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
  # Local state on purpose: this stack creates the remote-state bucket. Run once per account
  # (management, staging, pack-prod, auto-prod) and keep the tiny local state file in a safe place
  # (or migrate it into the bucket it created afterwards).
}

provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]

  default_tags {
    tags = {
      owner         = "kairox"
      env           = var.env
      company       = var.company
      "cost-center" = var.cost_center
      managed-by    = "terraform"
    }
  }
}
