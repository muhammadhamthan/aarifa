terraform {
  required_version = ">= 1.13"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0, < 7.0"
      # aws.dr is only used when var.dr_copy_region is set (cross-region backup copy, client consent).
      configuration_aliases = [aws.dr]
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.6"
    }
  }
}
