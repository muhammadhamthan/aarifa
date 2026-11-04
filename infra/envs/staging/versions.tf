terraform {
  required_version = ">= 1.13"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.7"
    }
  }

  # Partial config: bucket/key/region/use_lockfile come from backend.hcl
  #   terraform init -backend-config=backend.hcl
  backend "s3" {}
}

provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]

  default_tags {
    tags = local.tags
  }
}

# Only used when dr_copy_region is set (encrypted backup replication, client consent required).
provider "aws" {
  alias               = "dr"
  region              = coalesce(var.dr_copy_region, var.region)
  allowed_account_ids = [var.account_id]

  default_tags {
    tags = local.tags
  }
}
