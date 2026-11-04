terraform {
  required_version = ">= 1.13"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # State lives in the management account's bucket (created with infra/bootstrap there).
  backend "s3" {}
}

# Run with management-account credentials. Organizations is a global service; the region only
# selects the API endpoint.
provider "aws" {
  region = var.home_region

  default_tags {
    tags = {
      owner         = "kairox"
      env           = "org"
      company       = "kairox"
      "cost-center" = "kairox-platform"
      managed-by    = "terraform"
    }
  }
}
